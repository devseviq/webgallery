from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .state import atomic_write, coerce_int


def markdown_escape(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        padded = row + [""] * (len(headers) - len(row))
        lines.append("| " + " | ".join(markdown_escape(cell) for cell in padded[: len(headers)]) + " |")
    return "\n".join(lines)


def render_markdown_list(items, *, empty_text: str, normalize_string_list: Callable[[object], list[str]]) -> str:
    normalized = items if isinstance(items, list) else normalize_string_list(items)
    if not normalized:
        return f"- {empty_text}"
    lines: list[str] = []
    for item in normalized:
        if isinstance(item, dict):
            lines.append(f"- `{item.get('file', item.get('letter', 'item'))}`: {json.dumps(item, ensure_ascii=False)}")
        else:
            lines.append(f"- {item}")
    return "\n".join(lines)


def render_dependency_graph(plan: dict) -> str:
    groups = plan.get("groups", {})
    if not groups:
        return "Group 0: (none)"
    lines: list[str] = []
    for group_key, agent_ids in sorted(groups.items(), key=lambda item: coerce_int(item[0])):
        lines.append(f"Group {group_key}: {', '.join(agent_ids) if agent_ids else '(none)'}")
    return "\n".join(lines)


def render_plan_doc(
    plan: dict,
    *,
    default_plan_fields: Callable[[dict], dict],
    refresh_plan_elements: Callable[[dict], None],
    normalize_string_list: Callable[[object], list[str]],
) -> str:
    plan = default_plan_fields(plan)
    refresh_plan_elements(plan)
    elements = plan["plan_elements"]
    lines = [
        f"# Campaign — {elements.get('campaign_title') or plan.get('description', '') or plan['id']}",
        "",
        f"**Plan ID:** {plan['id']}",
        f"**Date:** {plan.get('created_at', '')[:10]}",
        f"**Status:** {plan.get('status', 'draft')}",
        f"**Plan file:** {plan.get('plan_file', '')}",
        f"**Plan doc:** {plan.get('plan_doc', '')}",
    ]
    if plan.get("planner_kind"):
        lines.append(f"**Planner kind:** {plan['planner_kind']}")
    if plan.get("legacy_status"):
        lines.append(f"**Legacy status:** {plan['legacy_status']}")
    if plan.get("source_roadmap"):
        lines.append(f"**Source roadmap:** {plan['source_roadmap']}")
    discovery_docs = normalize_string_list(plan.get("source_discovery_docs", []))
    if discovery_docs:
        lines.append(f"**Source discovery docs:** {', '.join(discovery_docs)}")
    lines.extend(["", "---", "", "## 1. Goal", "", elements.get("goal_statement") or "TODO: add goal statement", ""])

    lines.extend(
        [
            "## 2. Exit Criteria",
            "",
            render_markdown_list(
                elements.get("exit_criteria", []),
                empty_text="No exit criteria defined.",
                normalize_string_list=normalize_string_list,
            ),
            "",
        ]
    )

    impact_rows: list[list[str]] = []
    for item in elements.get("impact_assessment", []):
        if isinstance(item, dict):
            impact_rows.append(
                [
                    str(item.get("file", "")),
                    str(item.get("lines", "")),
                    str(item.get("change_type", "")),
                    str(item.get("risk", "")),
                ]
            )
    lines.extend(["## 3. Impact Assessment", ""])
    lines.append(
        markdown_table(["File", "Current Lines", "Change Type", "Risk"], impact_rows) if impact_rows else "- No impact assessment recorded."
    )
    lines.append("")

    roster_rows: list[list[str]] = []
    for item in elements.get("agent_roster", []):
        if isinstance(item, dict):
            roster_rows.append(
                [
                    str(item.get("letter", "")),
                    str(item.get("name", "")),
                    str(item.get("scope", "")),
                    ", ".join(item.get("deps", [])) if isinstance(item.get("deps"), list) else str(item.get("deps", "")),
                    ", ".join(item.get("files", [])) if isinstance(item.get("files"), list) else str(item.get("files", "")),
                    str(item.get("group", "")),
                    str(item.get("complexity", "")),
                ]
            )
    lines.extend(["## 4. Agent Roster", ""])
    lines.append(
        markdown_table(["Letter", "Name", "Scope", "Deps", "Files Owned", "Group", "Complexity"], roster_rows)
        if roster_rows
        else "- No agents defined."
    )
    lines.append("")

    lines.extend(["## 5. Dependency Graph", "", "```text", render_dependency_graph(plan), "```", ""])

    ownership_rows: list[list[str]] = []
    for item in elements.get("file_ownership_map", []):
        if isinstance(item, dict):
            ownership_rows.append([str(item.get("file", "")), str(item.get("owner", ""))])
    lines.extend(["## 6. File Ownership Map", ""])
    lines.append(markdown_table(["File", "Owner"], ownership_rows) if ownership_rows else "- No file ownership map recorded.")
    lines.append("")

    conflict_rows: list[list[str]] = []
    for item in elements.get("conflict_zone_analysis", []):
        if isinstance(item, dict):
            conflict_rows.append(
                [
                    str(item.get("conflict_zone", item.get("files", ""))),
                    str(item.get("affected", "")),
                    str(item.get("mitigation", item.get("reason", ""))),
                ]
            )
        else:
            conflict_rows.append([str(item), "", ""])
    lines.extend(["## 7. Conflict Zone Analysis", ""])
    lines.append(
        markdown_table(["Conflict Zone", "Affected?", "Mitigation"], conflict_rows)
        if conflict_rows
        else "- No conflict zone analysis recorded."
    )
    lines.append("")

    lines.extend(
        [
            "## 8. Integration Points",
            "",
            render_markdown_list(
                elements.get("integration_points", []),
                empty_text="No cross-agent contracts.",
                normalize_string_list=normalize_string_list,
            ),
            "",
        ]
    )
    lines.extend(
        [
            "## 9. Schema Changes",
            "",
            render_markdown_list(
                elements.get("schema_changes", []),
                empty_text="No schema changes required.",
                normalize_string_list=normalize_string_list,
            ),
            "",
        ]
    )

    risk_rows: list[list[str]] = []
    for item in elements.get("risk_assessment", []):
        if isinstance(item, dict):
            risk_rows.append(
                [
                    str(item.get("risk", "")),
                    str(item.get("likelihood", "")),
                    str(item.get("impact", "")),
                    str(item.get("mitigation", "")),
                ]
            )
        else:
            risk_rows.append([str(item), "", "", ""])
    lines.extend(["## 10. Risk Assessment", ""])
    lines.append(markdown_table(["Risk", "Likelihood", "Impact", "Mitigation"], risk_rows) if risk_rows else "- No risks recorded.")
    lines.append("")

    lines.extend(
        [
            "## 11. Verification Strategy",
            "",
            render_markdown_list(
                elements.get("verification_strategy", []),
                empty_text="No verification strategy recorded.",
                normalize_string_list=normalize_string_list,
            ),
            "",
        ]
    )
    lines.extend(
        [
            "## 12. Documentation Updates",
            "",
            render_markdown_list(
                elements.get("documentation_updates", []),
                empty_text="No documentation updates required.",
                normalize_string_list=normalize_string_list,
            ),
            "",
        ]
    )

    if plan.get("phase") or plan.get("source_roadmap"):
        lines.extend(["", "## R1. Roadmap Phase", ""])
        roadmap_lines: list[str] = []
        if plan.get("phase"):
            roadmap_lines.append(f"Phase: {plan['phase']}")
        if plan.get("source_roadmap"):
            roadmap_lines.append(f"Roadmap reference: {plan['source_roadmap']}")
        lines.append("\n".join(roadmap_lines) if roadmap_lines else "No roadmap phase metadata.")

    if normalize_string_list(plan.get("behavioral_invariants", [])):
        lines.extend(
            [
                "",
                "## R2. Behavioral Invariants",
                "",
                render_markdown_list(
                    plan.get("behavioral_invariants", []),
                    empty_text="No behavioral invariants recorded.",
                    normalize_string_list=normalize_string_list,
                ),
            ]
        )

    if str(plan.get("rollback_strategy", "")).strip():
        lines.extend(["", "## R3. Rollback Strategy", "", str(plan.get("rollback_strategy", "")).strip()])

    return "\n".join(lines).rstrip() + "\n"


def write_plan_doc(
    plan: dict,
    *,
    safe_resolve: Callable,
    plan_doc_path: Callable,
    render_plan_doc_fn: Callable[[dict], str],
    relative_path: Callable,
) -> str:
    doc_path = safe_resolve(plan.get("plan_doc") or plan_doc_path(plan))
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(doc_path, render_plan_doc_fn(plan))
    return relative_path(doc_path)


def persist_plan_artifacts(
    plan: dict,
    *,
    default_plan_fields: Callable[[dict], dict],
    refresh_plan_elements: Callable[[dict], None],
    now_iso: Callable[[], str],
    write_plan_doc_fn: Callable[[dict], str],
    plan_file_path: Callable,
    atomic_write: Callable[[Path, str], None],
) -> dict:
    plan = dict(plan)
    plan = default_plan_fields(plan)
    refresh_plan_elements(plan)
    plan["updated_at"] = now_iso()
    plan["plan_doc"] = write_plan_doc_fn(plan)
    plan_path = plan_file_path(plan["id"])
    atomic_write(plan_path, json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    return plan
