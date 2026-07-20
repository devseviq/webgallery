from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .state import atomic_write, coerce_int

STATUS_SYMBOLS = {
    "done": "\u2713",
    "running": "\u25ba",
    "ready": "\u25cb",
    "pending": "\u00b7",
    "blocked": "\u2717",
    "failed": "\u2717",
}

STATUS_ORDER = ("ready", "running", "done", "failed", "blocked", "pending")
_VALID_MODELS = {"mini", "standard", "max"}
_DEFAULT_MODEL_MAP = {"low": "mini", "medium": "standard", "high": "max"}
_MAX_DEPENDENCY_DEPTH = 200

_SYSTEM_RULES_TEMPLATE = textwrap.dedent("""\
    You are executing an agent task.

    RULES:
    1. Read {conventions_file} first for project conventions.
    2. Follow the agent spec below exactly \u2014 do not exceed scope.
    3. Run ALL verification steps listed in the spec before finishing.
    4. When done, output a structured result:
""")

_AGENT_RESULT_SCHEMA = textwrap.dedent("""\
    AGENT_RESULT_JSON:
    {{
      "id": "{agent_id}",
      "name": "{agent_name}",
      "status": "done",
      "files_modified": ["list of files"],
      "tests_passed": 0,
      "tests_failed": 0,
      "issues": [],
      "input_tokens": 0,
      "output_tokens": 0,
      "summary": "1-2 sentence description"
    }}
""")


def _normalize_complexity(value: object) -> str:
    complexity = str(value or "").strip().lower()
    return complexity if complexity in _DEFAULT_MODEL_MAP else "low"


def resolve_model_for_task(task: dict, cfg: dict | None) -> str:
    models_cfg = cfg.get("models", {}) if isinstance(cfg, dict) else {}
    complexity = _normalize_complexity(task.get("complexity"))
    configured = str(models_cfg.get(complexity, _DEFAULT_MODEL_MAP[complexity]) or "").strip().lower()
    return configured if configured in _VALID_MODELS else "standard"


def compute_dependency_depths(
    deps_map: dict[str, list[str]],
    subject: str,
    *,
    error_type: type[Exception] = RuntimeError,
) -> dict[str, int]:
    missing: dict[str, list[str]] = {}
    for item_id, deps in deps_map.items():
        unknown = [dep for dep in deps if dep not in deps_map]
        if unknown:
            missing[item_id] = unknown
    if missing:
        details = "; ".join(f"{item.upper()} -> {', '.join(dep.upper() for dep in deps)}" for item, deps in sorted(missing.items()))
        raise error_type(f"Missing dependencies in {subject}: {details}")

    depths: dict[str, int] = {}
    active_stack: list[str] = []
    active_index: dict[str, int] = {}

    def depth_of(item_id: str) -> int:
        if item_id in depths:
            return depths[item_id]
        if item_id in active_index:
            cycle = active_stack[active_index[item_id] :] + [item_id]
            cycle_text = " -> ".join(node.upper() for node in cycle)
            raise error_type(f"Dependency cycle detected in {subject}: {cycle_text}")

        active_index[item_id] = len(active_stack)
        active_stack.append(item_id)
        deps = deps_map.get(item_id, [])
        depth = 0 if not deps else 1 + max(depth_of(dep) for dep in deps)
        if depth > _MAX_DEPENDENCY_DEPTH:
            raise error_type(f"Dependency graph in {subject} exceeds maximum depth {_MAX_DEPENDENCY_DEPTH}: {item_id.upper()}")
        active_stack.pop()
        active_index.pop(item_id, None)
        depths[item_id] = depth
        return depth

    for item_id in deps_map:
        depth_of(item_id)
    return depths


def assign_groups(
    state: dict,
    *,
    compute_dependency_depths_fn: Callable[[dict[str, list[str]], str], dict[str, int]],
) -> None:
    tasks = state["tasks"]
    deps_map = {task_id: list(task.get("deps", [])) for task_id, task in tasks.items()}
    depths = compute_dependency_depths_fn(deps_map, "task state")

    groups: dict[str, list[str]] = {}
    for task_id, depth in sorted(depths.items()):
        tasks[task_id]["group"] = depth
        groups.setdefault(str(depth), []).append(task_id)

    state["groups"] = groups


def recompute_ready(state: dict) -> None:
    done_ids = {task_id for task_id, task in state["tasks"].items() if task["status"] == "done"}
    for task in state["tasks"].values():
        if task["status"] in ("pending", "blocked"):
            task["status"] = "ready" if all(dep in done_ids for dep in task.get("deps", [])) else "blocked"


def sync_state(
    *,
    load_state_fn: Callable[[], dict],
    parse_spec_file_fn: Callable[[Path], dict],
    parse_tracker_fn: Callable[[], dict],
    build_tracker_prefix_map_fn: Callable[[dict], dict[str, str]],
    save_state_fn: Callable[[dict], None],
    assign_groups_fn: Callable[[dict], None],
    recompute_ready_fn: Callable[[dict], None],
    agents_dir: Path,
    new_task_factory: Callable[..., dict] | None = None,
    ensure_task_fields_fn: Callable[[dict], object] | None = None,
    error_type: type[Exception] = RuntimeError,
) -> dict:
    state = load_state_fn()
    sync_audit = state.get("sync_audit")
    if not isinstance(sync_audit, list):
        sync_audit = []
        state["sync_audit"] = sync_audit

    discovered: dict[str, tuple[str, Path]] = {}
    parsed_specs: dict[str, dict] = {}
    duplicates: dict[str, list[str]] = defaultdict(list)
    for spec in sorted(agents_dir.glob("agent-*-*.md")):
        match = re.match(r"agent-([a-z]+)-(.+)\.md", spec.name)
        if not match:
            continue
        letter, name = match.group(1), match.group(2)
        if letter in discovered:
            duplicates[letter].append(spec.name)
            continue
        discovered[letter] = (name, spec)

    if duplicates:
        duplicate_text = "; ".join(
            f"{letter.upper()} -> {', '.join([discovered[letter][1].name, *names])}" for letter, names in sorted(duplicates.items())
        )
        raise error_type(f"Duplicate agent IDs in specs: {duplicate_text}")

    discovered_ids = set(discovered)
    referenced_ids: set[str] = set()
    for letter, (_name, spec) in discovered.items():
        parsed = parse_spec_file_fn(spec)
        parsed_specs[letter] = parsed
        referenced_ids.update(parsed.get("deps", []))

    preserved_history: set[str] = set()
    pending_history = [task_id for task_id in sorted(referenced_ids) if task_id not in discovered_ids and task_id in state["tasks"]]
    while pending_history:
        task_id = pending_history.pop()
        if task_id in preserved_history or task_id in discovered_ids:
            continue
        task = state["tasks"].get(task_id)
        if not task:
            continue
        preserved_history.add(task_id)
        for dep in task.get("deps", []):
            if dep not in discovered_ids and dep in state["tasks"]:
                pending_history.append(dep)

    stale_ids = [task_id for task_id in list(state["tasks"]) if task_id not in discovered_ids and task_id not in preserved_history]
    for task_id in stale_ids:
        state["tasks"].pop(task_id, None)
        sync_audit.append({"task_id": task_id, "action": "removed", "reason": "spec_missing"})

    for letter, (name, spec) in discovered.items():
        parsed = parsed_specs[letter]
        if letter not in state["tasks"]:
            if new_task_factory:
                state["tasks"][letter] = new_task_factory(
                    letter,
                    name,
                    spec_file=parsed.get("spec_file", f"agents/{spec.name}"),
                    scope=parsed.get("scope", ""),
                    status="pending",
                    deps=parsed.get("deps", []),
                    files=parsed.get("files", []),
                    group=0,
                    complexity=parsed.get("complexity", "low"),
                )
            else:
                state["tasks"][letter] = {
                    "id": letter,
                    "name": name,
                    "spec_file": parsed.get("spec_file", f"agents/{spec.name}"),
                    "scope": parsed.get("scope", ""),
                    "status": "pending",
                    "deps": parsed.get("deps", []),
                    "files": parsed.get("files", []),
                    "group": 0,
                    "complexity": _normalize_complexity(parsed.get("complexity")),
                    "tracker_id": "",
                    "started_at": "",
                    "completed_at": "",
                    "summary": "",
                    "error": "",
                }
        else:
            task = state["tasks"][letter]
            task["name"] = name
            task["spec_file"] = parsed.get("spec_file", task.get("spec_file", f"agents/{spec.name}"))
            task["scope"] = parsed.get("scope", task.get("scope", ""))
            task["deps"] = parsed.get("deps", [])
            task["tracker_id"] = ""
            task["files"] = parsed.get("files", [])
            task["complexity"] = _normalize_complexity(parsed.get("complexity", task.get("complexity", "low")))
            if ensure_task_fields_fn:
                ensure_task_fields_fn(task)

    for task_id in preserved_history:
        task = state["tasks"][task_id]
        task.setdefault("id", task_id)
        task.setdefault("name", f"historical-{task_id}")
        task.setdefault("spec_file", "")
        task.setdefault("scope", "")
        task.setdefault("status", "pending")
        task.setdefault("deps", [])
        task.setdefault("files", [])
        task.setdefault("group", 0)
        task.setdefault("complexity", "low")
        task.setdefault("tracker_id", "")
        task.setdefault("started_at", "")
        task.setdefault("completed_at", "")
        task.setdefault("summary", "")
        task.setdefault("error", "")
        if ensure_task_fields_fn:
            ensure_task_fields_fn(task)

    tracker = parse_tracker_fn()
    prefix_map = build_tracker_prefix_map_fn(state)
    for tracker_id, entry in tracker.items():
        prefix = tracker_id.rsplit("-", 1)[0]
        agent_letter = prefix_map.get(prefix)
        if agent_letter and agent_letter in state["tasks"]:
            task = state["tasks"][agent_letter]
            task["tracker_id"] = tracker_id
            if entry["status"] == "done":
                task["status"] = "done"
                task["summary"] = entry.get("update", "")
            elif entry["status"] == "running":
                task["status"] = "running"
            elif entry["status"] == "failed":
                task["status"] = "failed"
                task["error"] = entry.get("issue", "") or entry.get("update", "")
            if ensure_task_fields_fn:
                ensure_task_fields_fn(task)

    assign_groups_fn(state)
    recompute_ready_fn(state)

    save_state_fn(state)
    return state


def emit_json(payload: dict | list) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def build_agent_prompt(task: dict, spec_text: str, *, conventions_file: str) -> str:
    rules = _SYSTEM_RULES_TEMPLATE.format(conventions_file=conventions_file)
    schema = _AGENT_RESULT_SCHEMA.format(
        agent_id=task["id"].upper(),
        agent_name=task["name"],
    )
    return f"{rules}\n{schema}\n--- AGENT SPEC START ---\n{spec_text}\n--- AGENT SPEC END ---\n"


def cmd_sync(_args, *, sync_state_fn: Callable[[], dict], state_file: Path) -> None:
    state = sync_state_fn()
    tasks = state["tasks"]
    done_count = sum(1 for task in tasks.values() if task["status"] == "done")
    ready_count = sum(1 for task in tasks.values() if task["status"] == "ready")
    print(f"Synced: {len(tasks)} tasks ({done_count} done, {ready_count} ready)")
    print(f"State: {state_file}")


def _status_counts(tasks: dict[str, dict]) -> dict[str, int]:
    counts = {status: 0 for status in STATUS_ORDER}
    for task in tasks.values():
        status = str(task.get("status", "") or "")
        counts.setdefault(status, 0)
        counts[status] += 1
    counts["total"] = sum(counts[status] for status in counts if status != "total")
    return counts


def _status_agents(tasks: dict[str, dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {status: [] for status in STATUS_ORDER}
    for task in sorted(tasks.values(), key=lambda item: item["id"]):
        status = str(task.get("status", "") or "")
        grouped.setdefault(status, [])
        grouped[status].append(
            {
                "id": task["id"],
                "name": task["name"],
                "group": coerce_int(task.get("group", 0) or 0),
                "deps": list(task.get("deps", [])),
                "spec_file": str(task.get("spec_file", "") or ""),
            }
        )
    return grouped


def _derive_status_payload(tasks: dict[str, dict], manifest: dict) -> tuple[str, str]:
    counts = _status_counts(tasks)
    manifest_status = str(manifest.get("status", "") or "")
    if manifest_status:
        status = manifest_status
    else:
        merge = manifest.get("merge", {}) if isinstance(manifest.get("merge"), dict) else {}
        verify = manifest.get("verify", {}) if isinstance(manifest.get("verify"), dict) else {}
        merge_status = str(merge.get("status", "") or "")
        verify_status = str(verify.get("status", "") or "")
        if counts["running"]:
            status = "awaiting_results"
        elif counts["failed"] or counts["blocked"]:
            status = "blocked"
        elif counts["ready"]:
            status = "ready"
        elif verify_status == "passed":
            status = "verified"
        elif verify_status == "failed":
            status = "verification_failed"
        elif merge_status == "conflicts":
            status = "merge_conflicts"
        elif merge_status in {"merged", "noop", "already_merged"}:
            status = "ready_for_verify"
        elif counts["done"]:
            status = "ready_for_merge"
        else:
            status = "idle"

    merge = manifest.get("merge", {}) if isinstance(manifest.get("merge"), dict) else {}
    verify = manifest.get("verify", {}) if isinstance(manifest.get("verify"), dict) else {}
    merge_status = str(merge.get("status", "") or "")
    verify_status = str(verify.get("status", "") or "")
    if counts["ready"]:
        next_action = "run_ready"
    elif counts["running"]:
        next_action = "await_results"
    elif counts["failed"] or counts["blocked"] or counts["pending"]:
        next_action = "review_blockers"
    elif merge_status in {"merged", "noop", "already_merged"} and verify_status != "passed":
        next_action = "verify"
    elif counts["done"]:
        next_action = "merge"
    elif status == "verified":
        next_action = "done"
    else:
        next_action = "idle"
    return status, next_action


def _status_payload(state: dict, cfg: dict) -> dict:
    tasks = state.get("tasks", {})
    manifest = state.get("execution_manifest", {}) if isinstance(state.get("execution_manifest"), dict) else {}
    status, next_action = _derive_status_payload(tasks, manifest)
    return {
        "project": str(cfg.get("project", {}).get("name", "Campaign") or "Campaign"),
        "plan_id": str(manifest.get("plan_id", "") or ""),
        "status": status,
        "updated_at": str(manifest.get("updated_at", "") or state.get("updated_at", "") or ""),
        "counts": _status_counts(tasks),
        "agents": _status_agents(tasks),
        "launch": dict(manifest.get("launch", {})) if isinstance(manifest.get("launch"), dict) else {},
        "merge": dict(manifest.get("merge", {})) if isinstance(manifest.get("merge"), dict) else {},
        "verify": dict(manifest.get("verify", {})) if isinstance(manifest.get("verify"), dict) else {},
        "next_action": next_action,
    }


def cmd_status(
    args,
    *,
    sync_state_fn: Callable[[], dict],
    cfg: dict,
    sym_map: dict[str, str],
    emit_json_fn: Callable[[dict | list], None] | None = None,
) -> None:
    state = sync_state_fn()
    tasks = state["tasks"]
    if not tasks:
        if getattr(args, "json", False) and emit_json_fn:
            emit_json_fn(_status_payload(state, cfg))
            return
        print("No tasks. Run 'sync' or add agent specs to agents/.")
        return

    if getattr(args, "json", False):
        if not emit_json_fn:
            raise RuntimeError("emit_json_fn is required for status --json")
        emit_json_fn(_status_payload(state, cfg))
        return

    by_group: dict[int, list[dict]] = {}
    for task in sorted(tasks.values(), key=lambda item: item["id"]):
        by_group.setdefault(task.get("group", 0), []).append(task)

    project_name = cfg.get("project", {}).get("name", "Campaign")
    print("=" * 74)
    print(f"  {project_name} \u2014 Task Status")
    print("=" * 74)

    for group in sorted(by_group):
        print(f"\n  Group {group}:")
        for task in by_group[group]:
            sym = sym_map.get(task["status"], "?")
            deps = f" (deps: {','.join(dep.upper() for dep in task['deps'])})" if task.get("deps") else ""
            tracker = f" [{task['tracker_id']}]" if task.get("tracker_id") else ""
            print(f"    {sym} Agent {task['id'].upper()} \u2014 {task['name']}{deps}{tracker}  [{task['status']}]")
            if task.get("scope"):
                print(f"      {task['scope'][:72]}")

    counts: dict[str, int] = {}
    for task in tasks.values():
        counts[task["status"]] = counts.get(task["status"], 0) + 1

    print(f"\n{'─' * 74}")
    parts = " | ".join(f"{status.title()}: {count}" for status, count in sorted(counts.items()))
    print(f"  Total: {len(tasks)} | {parts}")
    if state.get("updated_at"):
        print(f"  Last sync: {state['updated_at']}")
    print("=" * 74)


def cmd_ready(args, *, sync_state_fn: Callable[[], dict], emit_json_fn: Callable[[dict | list], None]) -> None:
    state = sync_state_fn()

    ready = sorted((task for task in state["tasks"].values() if task["status"] == "ready"), key=lambda item: item["id"])
    blocked = sorted((task for task in state["tasks"].values() if task["status"] == "blocked"), key=lambda item: item["id"])
    done_ids = {task_id for task_id, task in state["tasks"].items() if task["status"] == "done"}

    if getattr(args, "json", False):
        emit_json_fn(
            {
                "ready": ready,
                "blocked": [
                    {
                        "id": task["id"],
                        "name": task["name"],
                        "pending_deps": [dep for dep in task.get("deps", []) if dep not in done_ids],
                    }
                    for task in blocked
                ],
                "summary": {
                    "total": len(state["tasks"]),
                    "ready": len(ready),
                    "blocked": len(blocked),
                    "done": sum(1 for task in state["tasks"].values() if task["status"] == "done"),
                },
            }
        )
        return

    if not ready:
        print("No agents ready to launch.")
        if blocked:
            print(f"\n{len(blocked)} blocked:")
            for task in blocked:
                pending = [dep for dep in task.get("deps", []) if dep not in done_ids]
                print(f"  Agent {task['id'].upper()} \u2014 waiting on: {', '.join(dep.upper() for dep in pending)}")
        return

    print(f"{len(ready)} agent(s) ready:\n")
    for task in ready:
        print(f"  \u25cb Agent {task['id'].upper()} \u2014 {task['name']}")
        print(f"    Scope: {task.get('scope', 'N/A')[:72]}")
        print(f"    Spec:  {task.get('spec_file', 'N/A')}")
        print(f"    Files: {', '.join(task.get('files', [])) or 'N/A'}")
        print()


def cmd_run(
    args,
    *,
    sync_state_fn: Callable[[], dict],
    now_iso_fn: Callable[[], str],
    safe_resolve_fn: Callable[[str], Path],
    validate_spec_file_fn: Callable[[Path, str, bool], list[str]],
    save_state_fn: Callable[[dict], None],
    build_agent_prompt_fn: Callable[[dict, str], str],
    emit_json_fn: Callable[[dict | list], None],
    cfg: dict | None = None,
    ensure_task_fields_fn: Callable[[dict], object] | None = None,
) -> None:
    state = sync_state_fn()

    if args.agents == "ready":
        agent_ids = [task["id"] for task in state["tasks"].values() if task["status"] == "ready"]
    elif args.agents == "all":
        agent_ids = [task["id"] for task in state["tasks"].values() if task["status"] in ("ready", "pending")]
    else:
        agent_ids = [agent.strip().lower() for agent in args.agents.split(",")]

    now = now_iso_fn()
    launched: list[dict] = []
    skipped: list[dict] = []

    for agent_id in agent_ids:
        task = state["tasks"].get(agent_id)
        if not task:
            skipped.append({"id": agent_id, "reason": "not_found", "detail": f"Agent {agent_id.upper()} not found"})
            continue
        if task["status"] == "done":
            skipped.append({"id": agent_id, "reason": "already_done", "detail": f"Agent {agent_id.upper()} already done"})
            continue
        if task["status"] == "running":
            skipped.append({"id": agent_id, "reason": "already_running", "detail": f"Agent {agent_id.upper()} already running"})
            continue
        if task["status"] == "blocked":
            done_ids = {task_id for task_id, item in state["tasks"].items() if item["status"] == "done"}
            pending = [dep for dep in task.get("deps", []) if dep not in done_ids]
            skipped.append(
                {
                    "id": agent_id,
                    "reason": "blocked",
                    "pending_deps": pending,
                    "detail": f"Agent {agent_id.upper()} blocked on {', '.join(dep.upper() for dep in pending)}",
                }
            )
            continue

        spec_path = safe_resolve_fn(task["spec_file"])
        spec_errors = validate_spec_file_fn(spec_path, agent_id, True)
        if spec_errors:
            skipped.append(
                {
                    "id": agent_id,
                    "reason": "invalid_spec",
                    "errors": spec_errors,
                    "detail": "; ".join(spec_errors),
                }
            )
            continue

        if ensure_task_fields_fn:
            ensure_task_fields_fn(task)
        task["status"] = "running"
        task["started_at"] = now
        launched.append(task)

    save_state_fn(state)

    agents_payload: list[dict[str, Any]] = []
    output: dict[str, Any] = {
        "action": "launch",
        "timestamp": now,
        "requested": agent_ids,
        "launched": [task["id"] for task in launched],
        "skipped": skipped,
        "agents": agents_payload,
    }

    for task in launched:
        spec_path = safe_resolve_fn(task["spec_file"])
        try:
            spec_text = spec_path.read_text(encoding="utf-8") if spec_path.exists() else ""
        except OSError:
            spec_text = ""
        agents_payload.append(
            {
                "id": task["id"],
                "name": task["name"],
                "spec_file": task["spec_file"],
                "model": resolve_model_for_task(task, cfg),
                "isolation": "worktree",
                "background": True,
                "prompt": build_agent_prompt_fn(task, spec_text),
            }
        )

    emit_json_fn(output)


def cmd_complete(
    args,
    *,
    load_state_fn: Callable[[], dict],
    now_iso_fn: Callable[[], str],
    recompute_ready_fn: Callable[[dict], None],
    save_state_fn: Callable[[dict], None],
    ensure_task_fields_fn: Callable[[dict], None] | None = None,
    empty_merge_record_factory: Callable[[], dict] | None = None,
) -> None:
    state = load_state_fn()
    agent_id = args.agent.lower()
    task = state["tasks"].get(agent_id)

    if not task:
        print(f"Agent {agent_id.upper()} not found.")
        sys.exit(1)

    if ensure_task_fields_fn:
        ensure_task_fields_fn(task)
    task["status"] = "done"
    task["completed_at"] = now_iso_fn()
    task["summary"] = args.summary or ""
    if isinstance(task.get("agent_result"), dict):
        task["agent_result"]["status"] = "done"
        task["agent_result"]["summary"] = args.summary or task["agent_result"].get("summary", "")
        task["agent_result"]["reported_at"] = task["completed_at"]
    if empty_merge_record_factory:
        task["merge"] = empty_merge_record_factory()

    recompute_ready_fn(state)
    save_state_fn(state)

    newly_ready = [item for item in state["tasks"].values() if item["status"] == "ready" and item["id"] != agent_id]
    print(f"Agent {agent_id.upper()} \u2192 done.")
    if newly_ready:
        print("\nNewly ready:")
        for item in newly_ready:
            print(f"  \u25cb Agent {item['id'].upper()} \u2014 {item['name']}")


def cmd_fail(
    args,
    *,
    load_state_fn: Callable[[], dict],
    now_iso_fn: Callable[[], str],
    save_state_fn: Callable[[dict], None],
    ensure_task_fields_fn: Callable[[dict], None] | None = None,
    normalize_string_list_fn: Callable[[object], list[str]] | None = None,
    empty_merge_record_factory: Callable[[], dict] | None = None,
) -> None:
    state = load_state_fn()
    agent_id = args.agent.lower()
    task = state["tasks"].get(agent_id)

    if not task:
        print(f"Agent {agent_id.upper()} not found.")
        sys.exit(1)

    if ensure_task_fields_fn:
        ensure_task_fields_fn(task)
    task["status"] = "failed"
    task["error"] = args.reason or ""
    task["completed_at"] = now_iso_fn()
    if isinstance(task.get("agent_result"), dict):
        task["agent_result"]["status"] = "failed"
        if normalize_string_list_fn:
            task["agent_result"]["issues"] = normalize_string_list_fn(args.reason or "")
        task["agent_result"]["reported_at"] = task["completed_at"]
    if empty_merge_record_factory:
        task["merge"] = empty_merge_record_factory()
    save_state_fn(state)
    print(f"Agent {agent_id.upper()} \u2192 failed: {args.reason or '(no reason)'}")


def cmd_reset(
    args,
    *,
    load_state_fn: Callable[[], dict],
    recompute_ready_fn: Callable[[dict], None],
    save_state_fn: Callable[[dict], None],
    ensure_task_fields_fn: Callable[[dict], None] | None = None,
    empty_agent_result_factory: Callable[[], dict] | None = None,
    empty_merge_record_factory: Callable[[], dict] | None = None,
) -> None:
    state = load_state_fn()
    agent_id = args.agent.lower()
    task = state["tasks"].get(agent_id)

    if not task:
        print(f"Agent {agent_id.upper()} not found.")
        sys.exit(1)

    if ensure_task_fields_fn:
        ensure_task_fields_fn(task)
    task["status"] = "pending"
    task["started_at"] = ""
    task["completed_at"] = ""
    task["summary"] = ""
    task["error"] = ""
    if empty_agent_result_factory:
        task["agent_result"] = empty_agent_result_factory()
    if empty_merge_record_factory:
        task["merge"] = empty_merge_record_factory()
    recompute_ready_fn(state)
    save_state_fn(state)
    print(f"Agent {agent_id.upper()} \u2192 {task['status']}.")


def cmd_graph(_args, *, sync_state_fn: Callable[[], dict], sym_map: dict[str, str]) -> None:
    state = sync_state_fn()
    tasks = state["tasks"]
    by_group: dict[int, list[dict]] = {}
    for task in tasks.values():
        by_group.setdefault(task.get("group", 0), []).append(task)

    print("\n  Dependency Graph\n")

    prev_width = 0
    for group in sorted(by_group):
        group_tasks = sorted(by_group[group], key=lambda item: item["id"])
        cells = [f"[{sym_map.get(task['status'], '?')} {task['id'].upper()}:{task['name'][:8]}]" for task in group_tasks]
        row = "  ".join(cells)

        if group > 0:
            connector_count = min(len(cells), prev_width) or 1
            pad = "         "
            print(f"  {pad}{'|         ' * connector_count}")
            print(f"  {pad}{'v         ' * connector_count}")

        print(f"  Grp {group}: {row}")
        prev_width = len(cells)

    print()


def cmd_next(_args, *, sync_state_fn: Callable[[], dict]) -> None:
    state = sync_state_fn()
    tasks = state["tasks"]

    running = [task for task in tasks.values() if task["status"] == "running"]
    ready = [task for task in tasks.values() if task["status"] == "ready"]
    done = [task for task in tasks.values() if task["status"] == "done"]
    total = len(tasks)

    print(f"\n  Progress: {len(done)}/{total} done\n")

    if running:
        print(f"  Running ({len(running)}):")
        for task in running:
            print(f"    \u25ba Agent {task['id'].upper()} \u2014 {task['name']}  (since {task.get('started_at', '?')[:19]})")

    if ready:
        print(f"\n  Ready to launch ({len(ready)}):")
        for task in sorted(ready, key=lambda item: item["id"]):
            print(f"    \u25cb Agent {task['id'].upper()} \u2014 {task['name']}")
        print("\n  \u2192 python scripts/task_manager.py run ready")
    elif not running:
        blocked = [task for task in tasks.values() if task["status"] in ("pending", "blocked")]
        if blocked:
            print("\n  All remaining agents are blocked. Check dependencies.")
        else:
            print("\n  All done!")


def cmd_add(
    args,
    *,
    sync_state_fn: Callable[[], dict],
    validate_agent_id_fn: Callable[[str], None],
    assign_groups_fn: Callable[[dict], None],
    recompute_ready_fn: Callable[[dict], None],
    save_state_fn: Callable[[dict], None],
    safe_resolve_fn: Callable[[str], Path],
    new_task_factory: Callable[..., dict] | None = None,
) -> None:
    state = sync_state_fn()
    letter = args.letter.lower()
    name = args.name.lower()
    complexity = _normalize_complexity(getattr(args, "complexity", "low"))
    validate_agent_id_fn(letter)

    if letter in state["tasks"]:
        print(f"Agent {letter.upper()} already exists.")
        sys.exit(1)

    deps = [dep.strip().lower() for dep in args.deps.split(",") if dep.strip()] if args.deps else []
    files = [file_path.strip() for file_path in args.files.split(",") if file_path.strip()] if args.files else []

    if new_task_factory:
        state["tasks"][letter] = new_task_factory(
            letter,
            name,
            spec_file=f"agents/agent-{letter}-{name}.md",
            scope=args.scope or "",
            status="pending",
            deps=deps,
            files=files,
            group=0,
            complexity=complexity,
        )
    else:
        state["tasks"][letter] = {
            "id": letter,
            "name": name,
            "spec_file": f"agents/agent-{letter}-{name}.md",
            "scope": args.scope or "",
            "status": "pending",
            "deps": deps,
            "files": files,
            "group": 0,
            "complexity": complexity,
            "tracker_id": "",
            "started_at": "",
            "completed_at": "",
            "summary": "",
            "error": "",
        }

    assign_groups_fn(state)
    recompute_ready_fn(state)
    save_state_fn(state)

    task = state["tasks"][letter]
    print(f"Added Agent {letter.upper()} \u2014 {name}  [{task['status']}]")
    if not safe_resolve_fn(task["spec_file"]).exists():
        print(f"  Spec file missing. Generate with: python scripts/task_manager.py template {letter} {name}")


def cmd_new(
    args,
    *,
    sync_state_fn: Callable[[], dict],
    next_agent_letter_fn: Callable[[dict], str],
    cmd_add_fn: Callable[[argparse.Namespace], None],
    cmd_template_fn: Callable[[argparse.Namespace], None],
) -> None:
    state = sync_state_fn()
    letter = next_agent_letter_fn(state)
    add_args = argparse.Namespace(
        letter=letter,
        name=args.name,
        scope=args.scope or "",
        deps=args.deps or "",
        files=args.files or "",
        complexity=getattr(args, "complexity", "low"),
    )
    cmd_add_fn(add_args)
    if not getattr(args, "no_template", False):
        template_args = argparse.Namespace(letter=letter, name=args.name, scope=args.scope or "")
        cmd_template_fn(template_args)


def cmd_template(
    args,
    *,
    validate_agent_id_fn: Callable[[str], None],
    agents_dir: Path,
    render_spec_template_fn: Callable[[str, str, str], str],
) -> None:
    letter = args.letter.lower()
    name = args.name.lower()
    scope = args.scope or f"Implement the assigned scope for Agent {letter.upper()}."
    validate_agent_id_fn(letter)

    spec_path = agents_dir / f"agent-{letter}-{name}.md"
    if spec_path.exists():
        print(f"Already exists: {spec_path}")
        sys.exit(1)

    spec_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(spec_path, render_spec_template_fn(letter, name, scope))
    print(f"Created: {spec_path}")
