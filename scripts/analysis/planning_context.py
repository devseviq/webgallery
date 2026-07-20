from __future__ import annotations

import copy
import json

from task_runtime.state import coerce_int


def synthesize_planning_context(analysis_v2: dict) -> dict:
    selection = analysis_v2.get("selection", {})
    inventory = analysis_v2.get("inventory", {})
    graphs = analysis_v2.get("graphs", {})
    signals = analysis_v2.get("signals", {})
    derived = analysis_v2.get("derived", {})

    conflict_zones = list(signals.get("conflict_zones", []))
    ui_surfaces = list(derived.get("ui_surfaces", []))
    ownership_summary = dict(derived.get("ownership_summary", {}))
    startup_projects = sorted(
        {surface.get("project", "") for surface in ui_surfaces if surface.get("kind") == "startup" and surface.get("project")}
    )
    packaging_projects = sorted(
        {surface.get("project", "") for surface in ui_surfaces if surface.get("kind") == "packaging" and surface.get("project")}
    )

    return {
        "analysis_health": _analysis_health(
            selection,
            ownership_summary,
            ui_surfaces,
            inventory.get("detected_stacks", []),
        ),
        "detected_stacks": list(inventory.get("detected_stacks", [])),
        "project_graph": graphs.get("project_graph", {"nodes": [], "edges": []}),
        "conflict_zones": conflict_zones,
        "ui_surfaces": ui_surfaces,
        "ownership_summary": ownership_summary,
        "priority_projects": {
            "startup": startup_projects,
            "packaging": packaging_projects,
        },
        "coordination_hotspots": _coordination_hotspots(conflict_zones, ui_surfaces),
    }


def _analysis_health(
    selection: dict,
    ownership_summary: dict,
    ui_surfaces: list[dict],
    detected_stacks: list[str],
) -> dict:
    requested = list(selection.get("requested", []))
    applied = list(selection.get("applied", []))
    skipped = list(selection.get("skipped", []))
    requested_optional = [name for name in requested if name != "basic"]
    applied_optional = [name for name in applied if name != "basic"]
    fallback_only = not applied_optional and bool(requested_optional)
    heuristic_only = applied == ["basic"]

    warnings: list[str] = []
    if fallback_only:
        warnings.append("Optional analysis providers did not contribute; planning data is heuristic-only.")
    elif skipped:
        warnings.append("Some optional analysis providers were skipped; review ownership and coordination surfaces carefully.")
    if coerce_int(ownership_summary.get("unassigned_file_count", 0) or 0) > 0:
        warnings.append("Unassigned files remain in the analysis inventory.")
    if not ui_surfaces and any(name in {"dotnet", "wpf", "winui", "xaml-ui"} for name in detected_stacks):
        warnings.append("No UI surfaces were derived from the final merged analysis.")

    return {
        "mode": selection.get("mode", ""),
        "requested_providers": requested,
        "applied_providers": applied,
        "skipped_providers": skipped,
        "partial_analysis": bool(skipped),
        "fallback_only": fallback_only,
        "heuristic_only": heuristic_only,
        "confidence": _analysis_confidence(selection, fallback_only),
        "warnings": warnings,
    }


def _analysis_confidence(selection: dict, fallback_only: bool) -> str:
    applied = list(selection.get("applied", []))
    skipped = list(selection.get("skipped", []))
    applied_optional = [name for name in applied if name != "basic"]

    if applied_optional and not skipped:
        return "high"
    if applied_optional:
        return "medium"
    if fallback_only:
        return "low"
    if applied:
        return "medium"
    return "low"


def _coordination_hotspots(conflict_zones: list[dict], ui_surfaces: list[dict]) -> list[dict]:
    hotspots: list[dict] = []
    seen: set[str] = set()

    for surface in ui_surfaces:
        hotspot = {
            "kind": surface.get("kind", ""),
            "project": surface.get("project", ""),
            "entry": surface.get("entry", ""),
            "files": sorted(surface.get("files", [])),
            "reason": _surface_reason(surface.get("kind", "")),
            "startup": bool(surface.get("startup")),
        }
        _append_hotspot(hotspots, seen, hotspot)

    for zone in conflict_zones:
        hotspot = {
            "kind": "conflict-zone",
            "project": "",
            "entry": "",
            "files": sorted(zone.get("files", [])),
            "reason": zone.get("reason", ""),
            "startup": False,
        }
        _append_hotspot(hotspots, seen, hotspot)

    return hotspots


def _append_hotspot(hotspots: list[dict], seen: set[str], hotspot: dict):
    if len(hotspot.get("files", [])) < 2:
        return
    key = json.dumps(hotspot, sort_keys=True, ensure_ascii=False)
    if key in seen:
        return
    seen.add(key)
    hotspots.append(hotspot)


def _surface_reason(kind: str) -> str:
    reasons = {
        "startup": "desktop startup surface",
        "shell": "desktop shell surface",
        "webui": "web ui surface",
        "resources": "shared desktop resource surface",
        "packaging": "desktop packaging surface",
        "process-manifest": "desktop process manifest surface",
    }
    return reasons.get(kind, kind or "derived coordination surface")


def _files_overlap(files_a: list[str], files_b: list[str]) -> bool:
    norm_a = {f.replace("\\", "/") for f in files_a}
    norm_b = {f.replace("\\", "/") for f in files_b}
    return bool(norm_a & norm_b)


def scope_planning_context_for_agent(planning_context: dict, agent_files: list[str]) -> dict:
    """Filter the global planning context to only data relevant to an agent's file set.

    Normalizes all paths to forward slashes before comparison.
    If agent_files is empty, returns a copy with all list fields emptied.
    Preserves analysis_health, detected_stacks, priority_projects as-is.
    """
    result = copy.deepcopy(planning_context)

    if not agent_files:
        result["conflict_zones"] = []
        result["ui_surfaces"] = []
        result["coordination_hotspots"] = []
        ownership = dict(planning_context.get("ownership_summary", {}))
        ownership["projects"] = []
        result["ownership_summary"] = ownership
        return result

    # Filter conflict_zones
    conflict_zones = planning_context.get("conflict_zones", [])
    result["conflict_zones"] = [zone for zone in conflict_zones if _files_overlap(agent_files, zone.get("files", []))]

    # Filter ui_surfaces
    ui_surfaces = planning_context.get("ui_surfaces", [])
    result["ui_surfaces"] = [surface for surface in ui_surfaces if _files_overlap(agent_files, surface.get("files", []))]

    # Filter coordination_hotspots
    coordination_hotspots = planning_context.get("coordination_hotspots", [])
    result["coordination_hotspots"] = [
        hotspot for hotspot in coordination_hotspots if _files_overlap(agent_files, hotspot.get("files", []))
    ]

    # Filter ownership_summary.projects
    ownership = dict(planning_context.get("ownership_summary", {}))
    projects = ownership.get("projects", [])
    ownership["projects"] = [project for project in projects if _files_overlap(agent_files, project.get("files", []))]
    result["ownership_summary"] = ownership

    return result
