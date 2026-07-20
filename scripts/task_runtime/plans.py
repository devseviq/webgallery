from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from typing import Callable

from .state import coerce_int


def _safe_int(value: object, field_name: str, error_type: type[Exception]) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise error_type(f"'{field_name}' must be an integer, got: {value!r}")


def empty_plan_elements(title: str = "", *, default_verification_strategy: list[str] | None = None) -> dict:
    return {
        "campaign_title": title,
        "goal_statement": "",
        "exit_criteria": [],
        "impact_assessment": [],
        "agent_roster": [],
        "dependency_graph": [],
        "file_ownership_map": [],
        "conflict_zone_analysis": [],
        "integration_points": [],
        "schema_changes": [],
        "risk_assessment": [],
        "verification_strategy": list(default_verification_strategy or []),
        "documentation_updates": ["No documentation updates required."],
    }


def plan_planning_context(plan: dict) -> dict:
    return dict(plan.get("analysis_summary", {}).get("planning_context", {}))


def planning_context_conflict_zone_analysis(planning_context: dict) -> list[dict]:
    records: list[dict] = []
    seen: set[str] = set()
    conflict_zones = planning_context.get("conflict_zones", [])
    hotspots = planning_context.get("coordination_hotspots", [])

    for conflict in conflict_zones:
        files = sorted(str(item).strip() for item in conflict.get("files", []) if str(item).strip())
        if len(files) < 2:
            continue
        reason = str(conflict.get("reason", "")).strip() or "planning context conflict zone"
        record = {
            "files": files,
            "reason": reason,
        }
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            records.append(record)

    for hotspot in hotspots:
        files = sorted(str(item).strip() for item in hotspot.get("files", []) if str(item).strip())
        if len(files) < 2:
            continue
        project = str(hotspot.get("project", "")).strip()
        entry = str(hotspot.get("entry", "")).strip()
        reason = str(hotspot.get("reason", "")).strip() or "coordination hotspot"
        conflict_zone = entry or ", ".join(files)
        affected = project or hotspot.get("kind", "") or "cross-agent coordination"
        if hotspot.get("startup"):
            affected = f"{affected} (startup)"
        record = {
            "conflict_zone": conflict_zone,
            "files": files,
            "affected": affected,
            "mitigation": f"Keep one owner for this {reason}.",
            "reason": reason,
        }
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            records.append(record)

    return records


def planning_context_integration_points(
    planning_context: dict,
    *,
    normalize_string_list: Callable[[object], list[str]],
) -> list[str]:
    points: list[str] = []
    seen: set[str] = set()
    priority = planning_context.get("priority_projects", {})
    startup_projects = normalize_string_list(priority.get("startup", []))
    packaging_projects = normalize_string_list(priority.get("packaging", []))
    ownership_summary = planning_context.get("ownership_summary", {})

    if startup_projects:
        points.append(f"Keep startup project ownership centralized during integration: {', '.join(startup_projects)}.")
    if packaging_projects:
        points.append(f"Keep packaging project ownership centralized during integration: {', '.join(packaging_projects)}.")

    for hotspot in planning_context.get("coordination_hotspots", []):
        kind = str(hotspot.get("kind", "")).strip()
        if kind in {"", "conflict-zone"}:
            continue
        files = sorted(str(item).strip() for item in hotspot.get("files", []) if str(item).strip())
        if len(files) < 2:
            continue
        target = str(hotspot.get("entry", "")).strip() or str(hotspot.get("project", "")).strip() or ", ".join(files)
        reason = str(hotspot.get("reason", "")).strip() or "coordination hotspot"
        points.append(f"Route changes for {target} through one owner because it is a {reason}.")

    unassigned = coerce_int(ownership_summary.get("unassigned_file_count", 0) or 0)
    if unassigned > 0:
        points.append(f"Assign ownership for the {unassigned} unassigned analysis files before execution to avoid drift.")

    deduped: list[str] = []
    for item in points:
        normalized = item.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def refresh_plan_elements(
    plan: dict,
    *,
    empty_plan_elements_factory: Callable[[str], dict],
    normalize_string_list: Callable[[object], list[str]],
) -> None:
    elements = plan.setdefault(
        "plan_elements",
        empty_plan_elements_factory(plan.get("description", "") or plan.get("id", "")),
    )
    if not elements.get("campaign_title"):
        elements["campaign_title"] = plan.get("description", "") or plan.get("id", "")

    agents = plan.get("agents", [])
    elements["agent_roster"] = [
        {
            "letter": agent.get("letter", ""),
            "name": agent.get("name", ""),
            "scope": agent.get("scope", ""),
            "deps": list(agent.get("deps", [])),
            "files": list(agent.get("files", [])),
            "group": agent.get("group", 0),
            "complexity": agent.get("complexity", ""),
        }
        for agent in agents
    ]
    elements["dependency_graph"] = [
        {"group": int(group), "agents": list(agent_ids)}
        for group, agent_ids in sorted(plan.get("groups", {}).items(), key=lambda item: int(item[0]))
    ]
    elements["file_ownership_map"] = [
        {"file": file_path, "owner": agent.get("letter", "")} for agent in agents for file_path in agent.get("files", [])
    ]
    planning_context = plan_planning_context(plan)
    if plan.get("conflicts"):
        elements["conflict_zone_analysis"] = plan["conflicts"]
    elif not elements.get("conflict_zone_analysis"):
        elements["conflict_zone_analysis"] = planning_context_conflict_zone_analysis(planning_context)
    if plan.get("integration_steps"):
        elements["integration_points"] = plan["integration_steps"]
    elif not elements.get("integration_points"):
        elements["integration_points"] = planning_context_integration_points(
            planning_context,
            normalize_string_list=normalize_string_list,
        )


def plan_summary(
    plan: dict,
    *,
    relative_path: Callable,
    plan_file_path: Callable,
    plan_doc_path: Callable,
) -> dict:
    return {
        "id": plan["id"],
        "status": plan.get("status", "draft"),
        "description": plan.get("description", ""),
        "created_at": plan.get("created_at", ""),
        "updated_at": plan.get("updated_at", ""),
        "next_letter": plan.get("next_letter", ""),
        "agent_count": len(plan.get("agents", [])),
        "plan_file": plan.get("plan_file", relative_path(plan_file_path(plan["id"]))),
        "plan_doc": plan.get("plan_doc", plan_doc_path(plan)),
        "legacy_status": plan.get("legacy_status", ""),
    }


def looks_like_full_plan(entry: dict) -> bool:
    return any(
        key in entry
        for key in (
            "agents",
            "analysis_summary",
            "conflicts",
            "integration_steps",
            "groups",
            "plan_elements",
        )
    )


def default_plan_fields(
    plan: dict,
    *,
    empty_plan_elements_factory: Callable[[str], dict],
    plan_default_verification_strategy: Callable[[], list[str]],
    slugify: Callable[[str], str],
    relative_path: Callable,
    plan_file_path: Callable,
    plan_doc_path: Callable,
    normalize_string_list: Callable[[object], list[str]],
) -> dict:
    normalized = dict(plan)
    normalized.setdefault("schema_version", 1)
    normalized.setdefault("artifact_version", 1)
    normalized.setdefault("planner_kind", "planner")
    normalized.setdefault("source_discovery_docs", [])
    normalized.setdefault("source_roadmap", "")
    normalized.setdefault("phase", "")
    normalized.setdefault("behavioral_invariants", [])
    normalized.setdefault("rollback_strategy", "")
    normalized.setdefault("legacy_status", "")
    normalized.setdefault("backfill_reasons", [])
    normalized.setdefault("approved_at", "")
    normalized.setdefault("executed_at", "")
    normalized.setdefault("slug", slugify(normalized.get("description", "") or normalized.get("id", "")))
    normalized["plan_doc"] = normalized.get("plan_doc") or plan_doc_path(normalized)
    normalized["plan_file"] = normalized.get("plan_file") or relative_path(plan_file_path(normalized["id"]))
    normalized["source_discovery_docs"] = normalize_string_list(normalized.get("source_discovery_docs", []))
    normalized["behavioral_invariants"] = normalize_string_list(normalized.get("behavioral_invariants", []))
    normalized["groups"] = dict(normalized.get("groups", {}))
    normalized["conflicts"] = normalize_string_list(normalized.get("conflicts", []))
    normalized["integration_steps"] = normalize_string_list(normalized.get("integration_steps", []))

    for agent in normalized.get("agents", []):
        agent["deps"] = normalize_string_list(agent.get("deps", []))
        agent["files"] = normalize_string_list(agent.get("files", []))

    elements = dict(
        normalized.get("plan_elements") or empty_plan_elements_factory(normalized.get("description", "") or normalized.get("id", ""))
    )
    for key in (
        "exit_criteria",
        "integration_points",
        "schema_changes",
        "verification_strategy",
        "documentation_updates",
    ):
        elements[key] = normalize_string_list(elements.get(key, []))
    for key in (
        "impact_assessment",
        "agent_roster",
        "dependency_graph",
        "file_ownership_map",
        "conflict_zone_analysis",
        "risk_assessment",
    ):
        current = elements.get(key, [])
        elements[key] = current if isinstance(current, list) else []
    elements["campaign_title"] = (
        str(elements.get("campaign_title", "")).strip() or normalized.get("description", "") or normalized.get("id", "")
    )
    elements["goal_statement"] = str(elements.get("goal_statement", "")).strip()
    if not elements["verification_strategy"]:
        elements["verification_strategy"] = plan_default_verification_strategy()
    if not elements["documentation_updates"]:
        elements["documentation_updates"] = ["No documentation updates required."]
    normalized["plan_elements"] = elements
    return normalized


def resolve_plan_summary(plans: list[dict], plan_id: str | None) -> dict | None:
    if not plans:
        print("No plans.")
        return None
    if plan_id:
        plan = next((item for item in plans if item["id"] == plan_id), None)
        if not plan:
            print(f"Plan {plan_id} not found.")
            return None
        return plan
    return plans[-1]


def cmd_plan_preflight(
    args,
    *,
    plan_preflight_payload_fn: Callable[[], dict],
    emit_json_fn: Callable[[dict | list], None],
    fix_actions: list[str] | None = None,
) -> None:
    payload = plan_preflight_payload_fn()
    if fix_actions:
        payload["fix_actions"] = fix_actions

    if getattr(args, "json", False):
        emit_json_fn(payload)
        return

    status = "ready" if payload["ready"] else "blocked"
    print(f"Plan preflight: {status}")
    if payload["errors"]:
        print("\nErrors:")
        for error in payload["errors"]:
            print(f"  - {error}")
    if payload["warnings"]:
        print("\nWarnings:")
        for warning in payload["warnings"]:
            print(f"  - {warning}")
    print("\nCommands:")
    for key, value in payload["commands"].items():
        print(f"  {key}: {value or '(not configured)'}")

    if payload["errors"]:
        raise SystemExit(1)


def cmd_plan_finalize(
    args,
    *,
    load_state_fn: Callable[[], dict],
    resolve_plan_summary_fn: Callable[[list[dict], str | None], dict | None],
    load_plan_from_summary_fn: Callable[[dict], dict],
    finalize_plan_updates_fn: Callable[[dict, object], tuple[dict, list[str], list[str], list[str]]],
    persist_plan_artifacts_fn: Callable[[dict], dict],
    upsert_plan_summary_fn: Callable[[dict, dict], None],
    save_state_fn: Callable[[dict], None],
    emit_json_fn: Callable[[dict | list], None],
    error_type: type[Exception] = RuntimeError,
) -> None:
    state = load_state_fn()
    summary = resolve_plan_summary_fn(state.get("plans", []), getattr(args, "plan_id", None))
    if not summary:
        return
    plan = load_plan_from_summary_fn(summary)

    if plan.get("status") == "executed":
        raise error_type(f"{plan['id']} is executed; finalize a draft or approved plan instead.")

    plan, updated_fields, errors, warnings = finalize_plan_updates_fn(plan, args)
    plan = persist_plan_artifacts_fn(plan)
    upsert_plan_summary_fn(state, plan)
    save_state_fn(state)

    payload = {
        "plan_id": plan["id"],
        "status": plan.get("status", ""),
        "plan_file": plan.get("plan_file", ""),
        "plan_doc": plan.get("plan_doc", ""),
        "updated_fields": updated_fields,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }

    if getattr(args, "json", False):
        emit_json_fn(payload)
        return

    print(f"Plan {plan['id']} finalized")
    if updated_fields:
        print(f"  Updated: {', '.join(updated_fields)}")
    else:
        print("  No fields changed.")
    if errors:
        print("  Validation: incomplete")
        for error in errors:
            print(f"    - {error}")
    else:
        print("  Validation: ready for approval")
    if warnings:
        print("  Warnings:")
        for warning in warnings:
            print(f"    - {warning}")


def cmd_plan_go(
    args,
    *,
    plan_preflight_payload_fn: Callable[[], dict],
    load_state_fn: Callable[[], dict],
    resolve_plan_summary_fn: Callable[[list[dict], str | None], dict | None],
    load_plan_from_summary_fn: Callable[[dict], dict],
    finalize_plan_updates_fn: Callable[[dict, object], tuple[dict, list[str], list[str], list[str]]],
    persist_plan_artifacts_fn: Callable[[dict], dict],
    upsert_plan_summary_fn: Callable[[dict, dict], None],
    save_state_fn: Callable[[dict], None],
    plan_approve_fn: Callable[[argparse.Namespace], None],
    plan_execute_fn: Callable[[argparse.Namespace], None],
    emit_json_fn: Callable[[dict | list], None],
    error_type: type[Exception] = RuntimeError,
) -> None:
    preflight = plan_preflight_payload_fn()
    if preflight["errors"]:
        error_text = "\n".join(f"- {item}" for item in preflight["errors"])
        raise error_type(f"Plan preflight failed:\n{error_text}")

    state = load_state_fn()
    summary = resolve_plan_summary_fn(state.get("plans", []), getattr(args, "plan_id", None))
    if not summary:
        return
    plan = load_plan_from_summary_fn(summary)
    if not plan.get("agents"):
        raise error_type(f"{plan['id']} has no agents. Add agents first.")

    plan, updated_fields, errors, warnings = finalize_plan_updates_fn(plan, args)
    plan = persist_plan_artifacts_fn(plan)
    upsert_plan_summary_fn(state, plan)
    save_state_fn(state)

    if errors:
        error_text = "\n".join(f"- {item}" for item in errors)
        raise error_type(f"{plan['id']} cannot continue after finalize:\n{error_text}")

    approve_out = io.StringIO()
    execute_out = io.StringIO()
    with redirect_stdout(approve_out):
        plan_approve_fn(argparse.Namespace(plan_id=plan["id"]))
    with redirect_stdout(execute_out):
        plan_execute_fn(argparse.Namespace(plan_id=plan["id"]))

    state = load_state_fn()
    final_summary = resolve_plan_summary_fn(state.get("plans", []), plan["id"])
    if not final_summary:
        raise error_type(f"Plan {plan['id']} was not found after execution.")
    final_plan = load_plan_from_summary_fn(final_summary)
    plan_agent_ids = {str(a.get("letter", "")).strip().lower() for a in final_plan.get("agents", [])}
    ready_agents = [
        {"id": task["id"], "name": task["name"]}
        for task in state.get("tasks", {}).values()
        if task.get("status") == "ready" and task["id"] in plan_agent_ids
    ]
    payload = {
        "plan_id": final_plan["id"],
        "status": final_plan.get("status", ""),
        "plan_file": final_plan.get("plan_file", ""),
        "plan_doc": final_plan.get("plan_doc", ""),
        "updated_fields": updated_fields,
        "warnings": warnings,
        "preflight": preflight,
        "ready_agents": ready_agents,
    }

    if getattr(args, "json", False):
        emit_json_fn(payload)
        return

    print(f"Plan {final_plan['id']} progressed through preflight, finalize, approve, and execute.")
    if updated_fields:
        print(f"  Finalized: {', '.join(updated_fields)}")
    if warnings:
        print("  Warnings:")
        for warning in warnings:
            print(f"    - {warning}")
    print(approve_out.getvalue().rstrip())
    print(execute_out.getvalue().rstrip())


def cmd_plan_validate(
    args,
    *,
    load_state_fn: Callable[[], dict],
    resolve_plan_summary_fn: Callable[[list[dict], str | None], dict | None],
    load_plan_from_summary_fn: Callable[[dict], dict],
    validate_plan_fn: Callable[[dict, bool], list[str]],
    plan_validation_warnings_fn: Callable[[dict], list[str]],
    emit_json_fn: Callable[[dict | list], None],
) -> None:
    state = load_state_fn()
    plan_id = getattr(args, "plan_id", None)
    summary = resolve_plan_summary_fn(state.get("plans", []), plan_id)
    if not summary:
        return
    plan = load_plan_from_summary_fn(summary)
    errors = validate_plan_fn(plan, True)
    warnings = plan_validation_warnings_fn(plan)
    payload = {
        "plan_id": plan["id"],
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
    }

    if getattr(args, "json", False):
        emit_json_fn(payload)
    else:
        print(f"Plan {plan['id']} validation")
        if errors:
            print("\nErrors:")
            for error in errors:
                print(f"  - {error}")
        else:
            print("  No validation errors.")
        if warnings:
            print("\nWarnings:")
            for warning in warnings:
                print(f"  - {warning}")

    if errors:
        raise SystemExit(1)


def cmd_plan_criteria(
    args,
    *,
    load_state_fn: Callable[[], dict],
    resolve_plan_summary_fn: Callable[[list[dict], str | None], dict | None],
    load_plan_from_summary_fn: Callable[[dict], dict],
    resolve_plan_for_verify_fn: Callable[[dict], dict | None],
    explain_verify_resolution_failure_fn: Callable[[dict], str],
    plan_exit_criteria_fn: Callable[[dict], list[str]],
    emit_json_fn: Callable[[dict | list], None],
    error_type: type[Exception] = RuntimeError,
) -> None:
    state = load_state_fn()
    plan_id = getattr(args, "plan_id", None)
    plan: dict | None
    if plan_id:
        summary = resolve_plan_summary_fn(state.get("plans", []), plan_id)
        if not summary:
            return
        plan = load_plan_from_summary_fn(summary)
    else:
        plan = resolve_plan_for_verify_fn(state)
        if not plan:
            raise error_type("No valid executable plan available for criteria lookup. " + explain_verify_resolution_failure_fn(state))
    assert plan is not None

    payload = {
        "plan_id": plan["id"],
        "status": plan.get("status", ""),
        "plan_file": plan.get("plan_file", ""),
        "plan_doc": plan.get("plan_doc", ""),
        "legacy_status": plan.get("legacy_status", ""),
        "valid": True,
        "criteria": plan_exit_criteria_fn(plan),
    }

    if getattr(args, "json", False):
        emit_json_fn(payload)
        return

    print(f"Plan {payload['plan_id']} exit criteria")
    if payload["criteria"]:
        for item in payload["criteria"]:
            print(f"  - {item}")
    else:
        print("  No exit criteria recorded.")


def cmd_plan_create(
    args,
    *,
    sync_state_fn: Callable[[], dict],
    next_plan_id_fn: Callable[[dict], str],
    next_agent_letter_fn: Callable[[dict], str],
    analyze_project_fn: Callable[[], dict],
    now_iso_fn: Callable[[], str],
    slugify_fn: Callable[[str], str],
    empty_plan_elements_factory: Callable[[str], dict],
    persist_plan_artifacts_fn: Callable[[dict], dict],
    upsert_plan_summary_fn: Callable[[dict, dict], None],
    save_state_fn: Callable[[dict], None],
    emit_json_fn: Callable[[dict | list], None],
) -> None:
    state = sync_state_fn()
    plan_id = next_plan_id_fn(state)
    description = getattr(args, "description", "") or ""
    next_letter = next_agent_letter_fn(state)

    analysis = analyze_project_fn()
    planning_context = analysis.get("analysis_v2", {}).get("planning_context", {})

    plan = {
        "id": plan_id,
        "schema_version": 1,
        "artifact_version": 1,
        "created_at": now_iso_fn(),
        "status": "draft",
        "description": description,
        "slug": slugify_fn(description or plan_id),
        "planner_kind": getattr(args, "planner_kind", "planner") or "planner",
        "source_discovery_docs": list(getattr(args, "discovery_doc", []) or []),
        "source_roadmap": getattr(args, "roadmap", "") or "",
        "phase": getattr(args, "phase", "") or "",
        "behavioral_invariants": list(getattr(args, "behavioral_invariant", []) or []),
        "rollback_strategy": getattr(args, "rollback_strategy", "") or "",
        "legacy_status": "",
        "next_letter": next_letter,
        "agents": [],
        "groups": {},
        "conflicts": [],
        "integration_steps": [],
        "plan_doc": "",
        "plan_file": "",
        "plan_elements": empty_plan_elements_factory(description),
        "analysis_summary": {
            "total_files": analysis["totals"]["files"],
            "total_lines": analysis["totals"]["lines"],
            "conflict_zones": analysis["conflict_zones"],
            "modules": {key: value["total_lines"] for key, value in analysis["modules"].items()},
            "detected_stacks": analysis.get("detected_stacks", []),
            "project_graph": analysis.get("project_graph", {"nodes": [], "edges": []}),
            "analysis_schema_version": analysis.get("analysis_v2", {}).get("schema_version", 1),
            "analysis_providers": [
                provider.get("name", "") for provider in analysis.get("analysis_v2", {}).get("providers", []) if provider.get("name")
            ],
            "analysis_health": planning_context.get("analysis_health", {}),
            "planning_context": planning_context,
        },
    }
    plan = persist_plan_artifacts_fn(plan)
    upsert_plan_summary_fn(state, plan)
    save_state_fn(state)

    output = {
        "action": "plan_created",
        "plan": plan,
        "analysis": analysis,
        "existing_tasks": {key: {"name": value["name"], "status": value["status"]} for key, value in state["tasks"].items()},
    }

    if getattr(args, "json", False):
        emit_json_fn(output)
        return

    print(f"  Created {plan_id}: {description or '(no description)'}")
    print("  Status: draft")
    print(f"  Next available letter: {next_letter.upper()}")
    print(f"  Plan file: {plan['plan_file']}")
    print(f"  Existing tasks: {len(state['tasks'])} ({sum(1 for task in state['tasks'].values() if task['status'] == 'done')} done)")
    print()
    print("  Add agents to the plan:")
    print(f'    python scripts/task_manager.py plan-add-agent {plan_id} {next_letter} <name> --scope "..." --deps "" --files "..."')
    print("  Finalize required plan elements:")
    print(f'    python scripts/task_manager.py plan finalize {plan_id} --goal "..." --exit-criterion "..."')
    print()
    print("  Or use /manager plan to auto-generate agents from the analysis.")
    print("  Use --json flag for machine-readable output.")


def cmd_plan_show(
    args,
    *,
    load_state_fn: Callable[[], dict],
    resolve_plan_summary_fn: Callable[[list[dict], str | None], dict | None],
    load_plan_from_summary_fn: Callable[[dict], dict],
    emit_json_fn: Callable[[dict | list], None],
    print_plan_fn: Callable[[dict], None],
) -> None:
    state = load_state_fn()
    plans = state.get("plans", [])
    if not plans:
        print('No plans. Create one with: python scripts/task_manager.py plan create "description"')
        return

    summary = resolve_plan_summary_fn(plans, getattr(args, "plan_id", None))
    if not summary:
        return
    plan = load_plan_from_summary_fn(summary)
    if getattr(args, "json", False):
        emit_json_fn(plan)
        return
    print_plan_fn(plan)


def cmd_plan_list(
    args,
    *,
    load_state_fn: Callable[[], dict],
    emit_json_fn: Callable[[dict | list], None],
) -> None:
    state = load_state_fn()
    plans = state.get("plans", [])
    if not plans:
        print("No plans.")
        return
    if getattr(args, "json", False):
        emit_json_fn(plans)
        return

    print(f"\n  Plans ({len(plans)}):\n")
    for plan in plans:
        agent_count = plan.get("agent_count", 0)
        print(
            f"    {plan['id']}  [{plan['status']}]  {plan.get('description', '')[:50]}  ({agent_count} agents)  {plan['created_at'][:19]}"
        )
    print()


def cmd_plan_approve(
    args,
    *,
    load_state_fn: Callable[[], dict],
    resolve_plan_summary_fn: Callable[[list[dict], str | None], dict | None],
    load_plan_from_summary_fn: Callable[[dict], dict],
    validate_plan_fn: Callable[[dict, bool], list[str]],
    now_iso_fn: Callable[[], str],
    persist_plan_artifacts_fn: Callable[[dict], dict],
    upsert_plan_summary_fn: Callable[[dict, dict], None],
    save_state_fn: Callable[[dict], None],
    error_type: type[Exception] = RuntimeError,
) -> None:
    state = load_state_fn()
    summary = resolve_plan_summary_fn(state.get("plans", []), getattr(args, "plan_id", None))
    if not summary:
        return
    plan = load_plan_from_summary_fn(summary)
    if not plan.get("agents"):
        raise error_type(f"{plan['id']} has no agents. Add agents first.")

    errors = validate_plan_fn(plan, True)
    if errors:
        error_text = "\n".join(f"- {error}" for error in errors)
        raise error_type(f"{plan['id']} failed validation:\n{error_text}")

    plan["status"] = "approved"
    plan["approved_at"] = now_iso_fn()
    plan = persist_plan_artifacts_fn(plan)
    upsert_plan_summary_fn(state, plan)
    save_state_fn(state)
    print(f"  {plan['id']} -> approved ({len(plan['agents'])} agents)")
    print("  Required plan elements are now locked for execution.")
    print(f"  Execute with: python scripts/task_manager.py plan execute {plan['id']}")


def cmd_plan_reject(
    args,
    *,
    load_state_fn: Callable[[], dict],
    resolve_plan_summary_fn: Callable[[list[dict], str | None], dict | None],
    load_plan_from_summary_fn: Callable[[dict], dict],
    persist_plan_artifacts_fn: Callable[[dict], dict],
    upsert_plan_summary_fn: Callable[[dict, dict], None],
    save_state_fn: Callable[[dict], None],
) -> None:
    state = load_state_fn()
    summary = resolve_plan_summary_fn(state.get("plans", []), getattr(args, "plan_id", None))
    if not summary:
        return
    plan = load_plan_from_summary_fn(summary)
    plan["status"] = "rejected"
    plan = persist_plan_artifacts_fn(plan)
    upsert_plan_summary_fn(state, plan)
    save_state_fn(state)
    print(f"  {plan['id']} -> rejected")


def cmd_plan_execute(
    args,
    *,
    load_state_fn: Callable[[], dict],
    resolve_plan_summary_fn: Callable[[list[dict], str | None], dict | None],
    load_plan_from_summary_fn: Callable[[dict], dict],
    validate_plan_fn: Callable[[dict, bool], list[str]],
    new_task_factory: Callable[..., dict],
    agents_dir,
    write_spec_template_fn: Callable[[object, dict], None],
    assign_groups_fn: Callable[[dict], None],
    recompute_ready_fn: Callable[[dict], None],
    now_iso_fn: Callable[[], str],
    refresh_plan_elements_fn: Callable[[dict], None],
    persist_plan_artifacts_fn: Callable[[dict], dict],
    upsert_plan_summary_fn: Callable[[dict, dict], None],
    save_state_fn: Callable[[dict], None],
    sym_map: dict[str, str],
    error_type: type[Exception] = RuntimeError,
) -> None:
    state = load_state_fn()
    summary = resolve_plan_summary_fn(state.get("plans", []), getattr(args, "plan_id", None))
    if not summary:
        return
    plan = load_plan_from_summary_fn(summary)

    if plan["status"] != "approved":
        raise error_type(f"{plan['id']} is {plan['status']}, cannot execute until approved.")

    agents = plan.get("agents", [])
    if not agents:
        raise error_type(f"{plan['id']} has no agents.")

    errors = validate_plan_fn(plan, True)
    if errors:
        error_text = "\n".join(f"- {error}" for error in errors)
        raise error_type(f"{plan['id']} failed validation:\n{error_text}")

    collisions = sorted(
        {str(agent.get("letter", "")).strip().lower() for agent in agents if str(agent.get("letter", "")).strip().lower() in state["tasks"]}
    )
    if collisions:
        raise error_type(
            f"{plan['id']} cannot execute because agent IDs already exist in task state: "
            f"{', '.join(letter.upper() for letter in collisions)}"
        )

    registered: list[str] = []
    for agent in agents:
        letter = agent["letter"]
        name = agent["name"]
        state["tasks"][letter] = new_task_factory(
            letter,
            name,
            spec_file=f"agents/agent-{letter}-{name}.md",
            scope=agent.get("scope", ""),
            status="pending",
            deps=agent.get("deps", []),
            files=agent.get("files", []),
            group=agent.get("group", 0),
            complexity=agent.get("complexity", "medium"),
        )

        spec_path = agents_dir / f"agent-{letter}-{name}.md"
        if not spec_path.exists():
            spec_agent = dict(agent)
            spec_agent["_plan"] = plan
            write_spec_template_fn(spec_path, spec_agent)

        registered.append(letter)

    assign_groups_fn(state)
    recompute_ready_fn(state)
    plan["status"] = "executed"
    plan["executed_at"] = now_iso_fn()
    refresh_plan_elements_fn(plan)
    plan = persist_plan_artifacts_fn(plan)
    upsert_plan_summary_fn(state, plan)
    save_state_fn(state)

    print(f"\n  Executed {plan['id']}: registered {len(registered)} agents")
    for letter in registered:
        task = state["tasks"][letter]
        print(f"    {sym_map.get(task['status'], '?')} Agent {letter.upper()} — {task['name']}  [{task['status']}]")

    ready = [task for task in state["tasks"].values() if task["status"] == "ready" and task["id"] in registered]
    if ready:
        ids = ",".join(task["id"] for task in ready)
        print(f"\n  Ready to launch: python scripts/task_manager.py run {ids}")


def cmd_plan_add_agent(
    args,
    *,
    load_state_fn: Callable[[], dict],
    resolve_plan_summary_fn: Callable[[list[dict], str | None], dict | None],
    load_plan_from_summary_fn: Callable[[dict], dict],
    validate_agent_id_fn: Callable[[str], None],
    default_plan_fields_fn: Callable[[dict], dict],
    plan_assign_groups_fn: Callable[[dict, bool], None],
    validate_plan_fn: Callable[[dict, bool], list[str]],
    next_agent_letter_fn: Callable[[dict], str],
    persist_plan_artifacts_fn: Callable[[dict], dict],
    upsert_plan_summary_fn: Callable[[dict, dict], None],
    save_state_fn: Callable[[dict], None],
    error_type: type[Exception] = RuntimeError,
) -> None:
    state = load_state_fn()
    summary = resolve_plan_summary_fn(state.get("plans", []), args.plan_id)
    if not summary:
        return
    plan = load_plan_from_summary_fn(summary)

    if plan["status"] not in ("draft",):
        raise error_type(f"{plan['id']} is {plan['status']}, can only add agents to draft plans.")

    agent = {
        "letter": args.letter.lower(),
        "name": args.name.lower(),
        "scope": args.scope or "",
        "deps": [dep.strip().lower() for dep in args.deps.split(",") if dep.strip()] if args.deps else [],
        "files": [file_path.strip() for file_path in args.files.split(",") if file_path.strip()] if args.files else [],
        "group": _safe_int(args.group, "group", error_type) if args.group else 0,
        "complexity": args.complexity or "medium",
    }
    validate_agent_id_fn(agent["letter"])

    existing = [item["letter"] for item in plan.get("agents", [])]
    if agent["letter"] in existing:
        print(f"  Agent {agent['letter'].upper()} already in plan.")
        return
    if agent["letter"] in state.get("tasks", {}):
        raise error_type(f"Agent {agent['letter'].upper()} already exists in task state.")

    plan = default_plan_fields_fn(plan)
    plan.setdefault("agents", []).append(agent)
    plan_assign_groups_fn(plan, True)
    errors = validate_plan_fn(plan, False)
    if errors:
        error_text = "\n".join(f"- {error}" for error in errors)
        raise error_type(f"{plan['id']} agent update failed validation:\n{error_text}")

    plan["next_letter"] = next_agent_letter_fn(
        {"tasks": {**state.get("tasks", {}), **{item["letter"]: {} for item in plan.get("agents", [])}}}
    )
    plan = persist_plan_artifacts_fn(plan)
    upsert_plan_summary_fn(state, plan)
    save_state_fn(state)
    print(f"  Added Agent {agent['letter'].upper()} — {agent['name']} to {plan['id']}  [group {agent['group']}]")
