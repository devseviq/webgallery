from __future__ import annotations

from collections import defaultdict
from typing import Callable

from .state import coerce_int


def command_signature(command: str) -> str:
    tokens = command.replace("{files}", "").split()
    if not tokens:
        return ""
    if len(tokens) >= 3 and tokens[:3] == ["python", "-m", "pytest"]:
        return "python -m pytest"
    if len(tokens) >= 3 and tokens[:3] == ["python", "-m", "py_compile"]:
        return "python -m py_compile"
    if len(tokens) >= 2 and tokens[:2] == ["dotnet", "build"]:
        return "dotnet build"
    if len(tokens) >= 2:
        return " ".join(tokens[:2])
    return tokens[0]


def validation_contains_command(strategy: list[str], command: str) -> bool:
    normalized_command = command_signature(command)
    return any(
        normalized_command and (normalized_command in item or item in normalized_command or item.startswith(normalized_command))
        for item in strategy
    )


def validate_plan_elements(
    plan: dict,
    *,
    default_plan_fields: Callable[[dict], dict],
    normalize_string_list: Callable[[object], list[str]],
    commands_cfg: Callable[[], dict],
) -> list[str]:
    raw_elements = plan.get("plan_elements", {})
    plan = default_plan_fields(plan)
    elements = plan["plan_elements"]
    errors: list[str] = []

    required_text_fields = {
        "campaign_title": "campaign_title",
        "goal_statement": "goal_statement",
    }
    for key, label in required_text_fields.items():
        if not str(raw_elements.get(key, "")).strip():
            errors.append(f"Missing required plan element: {label}")

    for key in ("exit_criteria", "verification_strategy", "documentation_updates"):
        values = normalize_string_list(raw_elements.get(key, []))
        if not values:
            errors.append(f"Missing required plan element: {key}")

    strategy = normalize_string_list(elements.get("verification_strategy", []))
    if strategy:
        test_command = str(commands_cfg().get("test", "")).strip()
        if test_command and not validation_contains_command(strategy, test_command):
            errors.append("verification_strategy missing configured test command")

    return errors


def validate_agent_roster(
    plan: dict,
    *,
    default_plan_fields: Callable[[dict], dict],
    normalize_string_list: Callable[[object], list[str]],
    compute_dependency_depths: Callable,
    error_type: type[Exception],
    strict: bool = True,
) -> list[str]:
    plan = default_plan_fields(plan)
    errors: list[str] = []
    agents = plan.get("agents", [])
    seen_letters: set[str] = set()

    for agent in agents:
        letter = str(agent.get("letter", "")).strip().lower()
        name = str(agent.get("name", "")).strip()
        if not letter:
            errors.append("Agent roster contains an entry with no letter")
            continue
        if letter in seen_letters:
            errors.append(f"Duplicate agent ID in plan: {letter.upper()}")
        seen_letters.add(letter)
        if not name:
            errors.append(f"Agent {letter.upper()} is missing a name")

    deps_map = {
        str(agent.get("letter", "")).strip().lower(): normalize_string_list(agent.get("deps", []))
        for agent in agents
        if agent.get("letter")
    }
    if deps_map:
        dropped_warnings: list[str] = []
        if not strict:
            all_letters = set(deps_map.keys())
            filtered_map: dict[str, list[str]] = {}
            for letter, deps in deps_map.items():
                kept = [dep for dep in deps if dep in all_letters]
                for dep in deps:
                    if dep not in all_letters:
                        dropped_warnings.append(f"Agent {letter.upper()} dep {dep.upper()} not found, dropped")
                filtered_map[letter] = kept
            deps_map = filtered_map
            for warning in dropped_warnings:
                print(f"  Warning: {warning}")
        try:
            compute_dependency_depths(deps_map, f"plan {plan.get('id', '?')}")
        except error_type as exc:
            errors.append(str(exc))

    return errors


def validate_file_ownership(
    plan: dict,
    *,
    default_plan_fields: Callable[[dict], dict],
    normalize_string_list: Callable[[object], list[str]],
) -> list[str]:
    plan = default_plan_fields(plan)
    errors: list[str] = []
    owners: dict[str, list[str]] = defaultdict(list)

    for agent in plan.get("agents", []):
        letter = str(agent.get("letter", "")).strip().lower()
        for file_path in normalize_string_list(agent.get("files", [])):
            owners[file_path].append(letter)

    for file_path, letters in sorted(owners.items()):
        unique_letters = sorted({letter for letter in letters if letter})
        if len(unique_letters) > 1:
            errors.append(f"Duplicate file ownership: {file_path} claimed by {', '.join(letter.upper() for letter in unique_letters)}")
    return errors


def plan_validation_warnings(
    plan: dict,
    *,
    default_plan_fields: Callable[[dict], dict],
    normalize_string_list: Callable[[object], list[str]],
    commands_cfg: Callable[[], dict],
    safe_resolve: Callable,
    plan_planning_context: Callable[[dict], dict],
) -> list[str]:
    plan = default_plan_fields(plan)
    warnings: list[str] = []
    elements = plan["plan_elements"]
    planning_context = plan_planning_context(plan)
    analysis_health = planning_context.get("analysis_health", {})
    ownership_summary = planning_context.get("ownership_summary", {})

    if plan.get("legacy_status"):
        warnings.append(f"Plan marked as legacy: {plan['legacy_status']}")

    for key in ("impact_assessment", "conflict_zone_analysis", "risk_assessment"):
        if not elements.get(key):
            warnings.append(f"Plan element is empty: {key}")

    if not elements.get("integration_points"):
        warnings.append("Plan element is empty: integration_points")

    strategy = normalize_string_list(elements.get("verification_strategy", []))
    for key in ("compile", "build"):
        command = str(commands_cfg().get(key, "")).strip()
        if command and strategy and not validation_contains_command(strategy, command):
            warnings.append(f"verification_strategy does not mention configured {key} command")

    for doc_path in normalize_string_list(plan.get("source_discovery_docs", [])):
        if not safe_resolve(doc_path).exists():
            warnings.append(f"Referenced discovery doc not found: {doc_path}")
    if plan.get("source_roadmap") and not safe_resolve(plan["source_roadmap"]).exists():
        warnings.append(f"Referenced roadmap not found: {plan['source_roadmap']}")

    for item in normalize_string_list(analysis_health.get("warnings", [])):
        warnings.append(f"Analysis health: {item}")

    unassigned = coerce_int(ownership_summary.get("unassigned_file_count", 0) or 0)
    if unassigned > 0:
        warnings.append(f"Analysis shows {unassigned} unassigned files; verify ownership before approval")

    return warnings


def validate_plan(
    plan: dict,
    *,
    validate_plan_elements_fn: Callable[[dict, bool], list[str]],
    validate_agent_roster_fn: Callable[[dict, bool], list[str]],
    validate_file_ownership_fn: Callable[[dict], list[str]],
    strict: bool = True,
) -> list[str]:
    errors: list[str] = []
    errors.extend(validate_plan_elements_fn(plan, strict))
    errors.extend(validate_agent_roster_fn(plan, strict))
    errors.extend(validate_file_ownership_fn(plan))
    return errors


def mark_plan_needs_backfill(
    plan: dict,
    *,
    normalize_string_list: Callable[[object], list[str]],
    validate_plan_fn: Callable[[dict, bool], list[str]],
) -> dict:
    plan = dict(plan)
    if plan.get("legacy_status") == "needs_backfill" and normalize_string_list(plan.get("backfill_reasons", [])):
        return plan

    elements = plan.get("plan_elements", {})
    reasons: list[str] = []

    if not str(elements.get("goal_statement", "")).strip():
        reasons.append("empty goal_statement")
    if not normalize_string_list(elements.get("exit_criteria", [])):
        reasons.append("empty exit_criteria")
    if not normalize_string_list(elements.get("verification_strategy", [])):
        reasons.append("empty verification_strategy")
    if not normalize_string_list(elements.get("documentation_updates", [])):
        reasons.append("empty documentation_updates")

    for error in validate_plan_fn(plan, True):
        if error not in reasons:
            reasons.append(error)

    if reasons and plan.get("status") not in ("draft",):
        plan["legacy_status"] = "needs_backfill"
        plan["backfill_reasons"] = reasons
    return plan


def backfill_legacy_plan(
    plan: dict,
    *,
    default_plan_fields: Callable[[dict], dict],
    refresh_plan_elements: Callable[[dict], None],
    mark_plan_needs_backfill_fn: Callable[[dict], dict],
) -> dict:
    normalized = default_plan_fields(plan)
    refresh_plan_elements(normalized)
    return mark_plan_needs_backfill_fn(normalized)
