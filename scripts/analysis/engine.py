from __future__ import annotations

import json
from typing import Callable, TypedDict

from task_runtime.state import coerce_int

from .basic_provider import _normalize_string_list, _summarize_detected_stacks, run_basic_analysis
from .derived import synthesize_ownership_summary, synthesize_ui_surfaces
from .dotnet_cli_provider import dotnet_cli_available, run_dotnet_cli_analysis
from .inventory import entry_project_memberships, set_entry_project_memberships
from .models import ANALYSIS_SCHEMA_VERSION, AnalysisRequest
from .planning_context import synthesize_planning_context
from .project_graph import refresh_project_inventory, synthesize_project_graph
from .relations import synthesize_dependency_edges
from .signals import synthesize_conflict_zones


class ProviderSpec(TypedDict):
    available: Callable[[AnalysisRequest, dict], tuple[bool, str]]
    run: Callable[[AnalysisRequest, dict], dict]


_PROVIDERS: dict[str, ProviderSpec] = {
    "basic": {
        "available": lambda request, current: (True, ""),
        "run": lambda request, current: run_basic_analysis(request),
    },
    "dotnet-cli": {
        "available": dotnet_cli_available,
        "run": run_dotnet_cli_analysis,
    },
}


def project_legacy_analysis(analysis_v2: dict) -> dict:
    """Project the v2 schema back to the legacy flattened response."""
    inventory = analysis_v2.get("inventory", {})
    graphs = analysis_v2.get("graphs", {})
    signals = analysis_v2.get("signals", {})

    return {
        "root": analysis_v2.get("root", ""),
        "analyzed_at": analysis_v2.get("generated_at", ""),
        "files": inventory.get("files", []),
        "dependency_edges": graphs.get("dependency_edges", []),
        "modules": inventory.get("modules", {}),
        "detected_stacks": inventory.get("detected_stacks", []),
        "project_graph": graphs.get("project_graph", {"nodes": [], "edges": []}),
        "conflict_zones": signals.get("conflict_zones", []),
        "totals": inventory.get("totals", {"files": 0, "lines": 0}),
    }


def run_analysis(request: AnalysisRequest) -> dict:
    """Run the configured analysis providers and return the legacy-compatible payload."""
    mode = _analysis_mode(request.cfg)
    requested = _selected_provider_names(request.cfg)
    analysis_v2 = _empty_analysis_payload(request, mode, requested)

    for name in requested:
        provider = _PROVIDERS.get(name)
        if provider is None:
            analysis_v2["selection"]["skipped"].append({"name": name, "reason": "unknown-provider"})
            continue

        available, reason = provider["available"](request, analysis_v2)
        if not available:
            analysis_v2["selection"]["skipped"].append({"name": name, "reason": reason or "not-available"})
            continue

        try:
            result = provider["run"](request, analysis_v2)
        except Exception as exc:
            analysis_v2["selection"]["skipped"].append({"name": name, "reason": f"error: {exc}"})
            continue

        meta = dict(result.get("provider", {}))
        meta["status"] = "applied"
        analysis_v2["providers"].append(meta)
        analysis_v2["selection"]["applied"].append(meta.get("name", name))
        _merge_provider_result(analysis_v2, result)

    if not analysis_v2["providers"]:
        fallback = run_basic_analysis(request)
        meta = dict(fallback.get("provider", {}))
        meta["status"] = "applied"
        analysis_v2["providers"].append(meta)
        analysis_v2["selection"]["applied"].append(meta.get("name", "basic"))
        _merge_provider_result(analysis_v2, fallback)

    _refresh_project_inventory(analysis_v2)
    _refresh_inventory_summary(analysis_v2)
    _refresh_project_graph(analysis_v2)
    _refresh_dependency_edges(analysis_v2)
    _refresh_signals(analysis_v2, request.cfg)
    _refresh_derived_views(analysis_v2)
    _refresh_planning_context(analysis_v2)

    legacy = project_legacy_analysis(analysis_v2)
    legacy["analysis_v2"] = analysis_v2
    return legacy


def _empty_analysis_payload(request: AnalysisRequest, mode: str, requested: list[str]) -> dict:
    return {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "root": str(request.root),
        "generated_at": request.generated_at,
        "providers": [],
        "selection": {
            "mode": mode,
            "requested": requested,
            "applied": [],
            "skipped": [],
        },
        "inventory": {
            "files": [],
            "modules": {},
            "detected_stacks": [],
            "totals": {"files": 0, "lines": 0},
        },
        "graphs": {
            "dependency_edges": [],
            "project_graph": {"nodes": [], "edges": []},
        },
        "signals": {
            "conflict_zones": [],
        },
        "derived": {
            "ui_surfaces": [],
            "ownership_summary": {
                "project_count": 0,
                "assigned_file_count": 0,
                "assigned_line_count": 0,
                "unassigned_file_count": 0,
                "unassigned_paths": [],
                "projects": [],
            },
        },
        "planning_context": {
            "analysis_health": {
                "mode": mode,
                "requested_providers": requested,
                "applied_providers": [],
                "skipped_providers": [],
                "partial_analysis": False,
                "fallback_only": False,
                "heuristic_only": False,
                "confidence": "low",
                "warnings": [],
            },
            "detected_stacks": [],
            "project_graph": {"nodes": [], "edges": []},
            "conflict_zones": [],
            "ui_surfaces": [],
            "ownership_summary": {
                "project_count": 0,
                "assigned_file_count": 0,
                "assigned_line_count": 0,
                "unassigned_file_count": 0,
                "unassigned_paths": [],
                "projects": [],
            },
            "priority_projects": {"startup": [], "packaging": []},
            "coordination_hotspots": [],
        },
    }


def _analysis_mode(cfg: dict) -> str:
    value = str(cfg.get("analysis", {}).get("mode", "auto")).strip().lower()
    return value if value in {"basic", "auto", "deep"} else "auto"


def _selected_provider_names(cfg: dict) -> list[str]:
    requested = _normalize_string_list(cfg.get("analysis", {}).get("providers", []))
    if _analysis_mode(cfg) == "basic":
        requested = ["basic"]
    elif not requested:
        requested = ["basic", "dotnet-cli"]

    ordered = ["basic", *requested]
    selected: list[str] = []
    for name in ordered:
        normalized = str(name).strip().lower()
        if normalized and normalized not in selected:
            selected.append(normalized)
    return selected


def _merge_provider_result(analysis_v2: dict, provider_result: dict):
    inventory = provider_result.get("inventory", {})
    graphs = provider_result.get("graphs", {})
    signals = provider_result.get("signals", {})

    analysis_v2["inventory"]["files"] = _merge_records(
        analysis_v2["inventory"].get("files", []),
        inventory.get("files", []),
        key_field="path",
    )
    analysis_v2["inventory"]["detected_stacks"] = _merge_unique_list(
        analysis_v2["inventory"].get("detected_stacks", []),
        inventory.get("detected_stacks", []),
    )
    analysis_v2["graphs"]["dependency_edges"] = _merge_edges(
        analysis_v2["graphs"].get("dependency_edges", []),
        graphs.get("dependency_edges", []),
    )
    analysis_v2["graphs"]["project_graph"]["nodes"] = _merge_records(
        analysis_v2["graphs"].get("project_graph", {}).get("nodes", []),
        graphs.get("project_graph", {}).get("nodes", []),
        key_field="id",
    )
    analysis_v2["graphs"]["project_graph"]["edges"] = _merge_edges(
        analysis_v2["graphs"].get("project_graph", {}).get("edges", []),
        graphs.get("project_graph", {}).get("edges", []),
    )
    analysis_v2["signals"]["conflict_zones"] = _merge_conflict_zones(
        analysis_v2["signals"].get("conflict_zones", []),
        signals.get("conflict_zones", []),
    )


def _refresh_inventory_summary(analysis_v2: dict):
    files = analysis_v2["inventory"].get("files", [])
    analysis_v2["inventory"]["modules"] = _summarize_modules(files)
    analysis_v2["inventory"]["totals"] = {
        "files": len(files),
        "lines": sum(coerce_int(entry.get("lines", 0) or 0) for entry in files),
    }
    analysis_v2["inventory"]["detected_stacks"] = _merge_unique_list(
        analysis_v2["inventory"].get("detected_stacks", []),
        _summarize_detected_stacks(files),
    )


def _refresh_project_inventory(analysis_v2: dict):
    analysis_v2["inventory"]["files"] = refresh_project_inventory(
        analysis_v2["inventory"].get("files", []),
    )


def _refresh_project_graph(analysis_v2: dict):
    analysis_v2["graphs"]["project_graph"] = synthesize_project_graph(
        analysis_v2["inventory"].get("files", []),
        analysis_v2["graphs"].get("project_graph", {"nodes": [], "edges": []}),
    )


def _refresh_signals(analysis_v2: dict, cfg: dict):
    synthesized = synthesize_conflict_zones(cfg, analysis_v2["inventory"].get("files", []))
    analysis_v2["signals"]["conflict_zones"] = _merge_conflict_zones(
        synthesized,
        analysis_v2["signals"].get("conflict_zones", []),
    )


def _refresh_dependency_edges(analysis_v2: dict):
    analysis_v2["graphs"]["dependency_edges"] = synthesize_dependency_edges(
        analysis_v2["inventory"].get("files", []),
        analysis_v2["graphs"].get("dependency_edges", []),
    )


def _refresh_derived_views(analysis_v2: dict):
    files = analysis_v2["inventory"].get("files", [])
    project_graph = analysis_v2["graphs"].get("project_graph", {"nodes": [], "edges": []})
    ui_surfaces = synthesize_ui_surfaces(files, project_graph)
    analysis_v2["derived"]["ui_surfaces"] = ui_surfaces
    analysis_v2["derived"]["ownership_summary"] = synthesize_ownership_summary(
        files,
        project_graph,
        ui_surfaces,
    )


def _refresh_planning_context(analysis_v2: dict):
    analysis_v2["planning_context"] = synthesize_planning_context(analysis_v2)


def _summarize_modules(files: list[dict]) -> dict[str, dict]:
    modules: dict[str, dict] = {}
    for entry in files:
        category = str(entry.get("category", "other") or "other")
        if category not in modules:
            modules[category] = {"file_count": 0, "total_lines": 0, "files": []}
        modules[category]["file_count"] += 1
        modules[category]["total_lines"] += coerce_int(entry.get("lines", 0) or 0)
        modules[category]["files"].append(entry["path"])
    return modules


def _merge_records(existing: list[dict], incoming: list[dict], *, key_field: str) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []

    for record in list(existing) + list(incoming):
        key = str(record.get(key_field, "")).strip()
        if not key:
            continue
        if key not in merged:
            merged[key] = dict(record)
            order.append(key)
            continue
        merged[key] = _merge_mapping(merged[key], record)

    return [merged[key] for key in order]


def _merge_mapping(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    merged_projects = _merge_unique_list(
        entry_project_memberships(existing),
        entry_project_memberships(incoming),
    )
    for key, value in incoming.items():
        if key in {"project", "project_memberships"}:
            continue
        if key not in merged:
            if value not in (None, "", [], {}):
                merged[key] = value
            continue
        merged[key] = _merge_values(merged[key], value)
    if merged_projects:
        set_entry_project_memberships(merged, merged_projects)
    return merged


def _merge_values(existing, incoming):
    if incoming in (None, "", [], {}):
        return existing
    if isinstance(existing, list) and isinstance(incoming, list):
        return _merge_unique_list(existing, incoming)
    if isinstance(existing, dict) and isinstance(incoming, dict):
        return _merge_mapping(existing, incoming)
    return incoming


def _merge_unique_list(existing: list, incoming: list) -> list:
    merged = list(existing)
    seen = {_stable_item_key(item) for item in merged}
    for item in incoming:
        key = _stable_item_key(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def _merge_edges(existing: list[dict], incoming: list[dict]) -> list[dict]:
    return _merge_unique_list(existing, incoming)


def _merge_conflict_zones(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_files: dict[str, dict] = {}
    for zone in existing + incoming:
        sorted_files = sorted(zone.get("files", []))
        files_key = json.dumps(sorted_files, sort_keys=True)
        reason = zone.get("reason", "")
        if files_key in by_files:
            prev_reason = by_files[files_key]["reason"]
            if reason and reason != prev_reason:
                by_files[files_key]["reason"] = f"{prev_reason}; {reason}" if prev_reason else reason
        else:
            by_files[files_key] = {"files": sorted_files, "reason": reason}
    return list(by_files.values())


def _stable_item_key(item) -> str:
    return json.dumps(item, sort_keys=True, ensure_ascii=False)
