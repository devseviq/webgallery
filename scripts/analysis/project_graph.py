from __future__ import annotations

import posixpath
from collections import defaultdict
from pathlib import PurePosixPath

from .inventory import entry_project_memberships, is_database_project_path, set_entry_project_memberships

_PROJECT_SUFFIXES = {".csproj", ".wapproj", ".vcxproj", ".sqlproj"}
_SOLUTION_SUFFIXES = {".sln", ".slnx"}


def refresh_project_inventory(files: list[dict]) -> list[dict]:
    file_index = {entry.get("path", ""): entry for entry in files if entry.get("path")}
    by_project: dict[str, list[dict]] = defaultdict(list)
    for entry in files:
        path = entry.get("path", "")
        memberships = entry_project_memberships(entry)
        if _looks_like_project_path(path):
            memberships = [path]
        set_entry_project_memberships(entry, memberships)
        for project_path in memberships:
            by_project[project_path].append(entry)

    for entry in files:
        path = entry.get("path", "")
        if _looks_like_project_path(path):
            _normalize_project_entry(entry)

    for project_path, project_files in by_project.items():
        project_entry = file_index.get(project_path)
        if not project_entry:
            continue
        _merge_project_assets(project_entry, project_files)

    return files


def synthesize_project_graph(files: list[dict], existing_graph: dict) -> dict:
    nodes = [dict(node) for node in existing_graph.get("nodes", [])]
    edges = [dict(edge) for edge in existing_graph.get("edges", [])]
    node_map: dict[str, dict] = {}
    order: list[str] = []

    for node in nodes:
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            continue
        if node.get("kind") == "project":
            node.pop("startup", None)
        if node.get("kind") == "solution":
            node.pop("startup_project", None)
            node.pop("startup_inference", None)
            node.pop("startup_candidates", None)
        node_map[node_id] = node
        order.append(node_id)

    for entry in files:
        path = entry.get("path", "")
        if not path:
            continue

        if _looks_like_solution_path(path):
            node = node_map.get(path, {"id": path, "kind": "solution", "name": PurePosixPath(path).stem, "path": path})
            solution_projects = entry.get("solution_projects", [])
            if solution_projects:
                node["project_count"] = len(solution_projects)
            node_map[path] = node
            if path not in order:
                order.append(path)
            continue

        if not _looks_like_project_path(path):
            continue

        node = node_map.get(path, _new_project_node(entry))
        node.setdefault("id", path)
        node.setdefault("kind", "project")
        node.setdefault("path", path)
        node["name"] = entry.get("assembly_name") or node.get("name") or PurePosixPath(path).stem
        node["project_kind"] = entry.get("project_kind") or node.get("project_kind") or _default_project_kind(path)

        for key in (
            "desktop_targets",
            "target_frameworks",
            "output_type",
            "project_role",
            "packaging_model",
            "app_xaml",
            "app_code_behind",
            "package_manifest",
            "package_identity",
            "package_entry_point",
            "package_references",
            "application_manifest_path",
        ):
            if entry.get(key):
                node[key] = entry[key]

        node_map[path] = node
        if path not in order:
            order.append(path)

    _refresh_solution_project_counts(node_map, edges)
    _infer_solution_startup(node_map, edges)
    return {"nodes": [node_map[node_id] for node_id in order], "edges": edges}


def _looks_like_project_path(path: str) -> bool:
    if not path:
        return False
    pure = PurePosixPath(path)
    return pure.suffix.lower() in _PROJECT_SUFFIXES or pure.name == "CMakeLists.txt"


def _looks_like_solution_path(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _SOLUTION_SUFFIXES


def _default_project_kind(path: str) -> str:
    if PurePosixPath(path).name == "CMakeLists.txt":
        return "cmake"
    if is_database_project_path(path):
        return "database"
    return "msbuild"


def _normalize_project_entry(entry: dict):
    if entry.get("appx_manifest") and not entry.get("package_manifest"):
        entry["package_manifest"] = _canonical_project_asset_path(entry.get("path", ""), entry["appx_manifest"])
    if entry.get("application_manifest") and not entry.get("application_manifest_path"):
        entry["application_manifest_path"] = _canonical_project_asset_path(entry.get("path", ""), entry["application_manifest"])


def _merge_project_assets(project_entry: dict, project_files: list[dict]):
    _normalize_project_entry(project_entry)
    for entry in project_files:
        path = entry.get("path", "")
        path_lower = path.lower()
        if path_lower.endswith(("app.xaml", "app.axaml")):
            project_entry["app_xaml"] = path
            if entry.get("code_behind"):
                project_entry["app_code_behind"] = entry["code_behind"]
        if path_lower.endswith(".appxmanifest"):
            project_entry["package_manifest"] = path
            for key in (
                "package_identity",
                "package_publisher",
                "package_version",
                "package_display_name",
                "package_entry_point",
                "package_executable",
                "package_application_id",
                "manifest_kind",
            ):
                if entry.get(key):
                    project_entry[key] = entry[key]
        if path_lower.endswith(".manifest"):
            project_entry["application_manifest_path"] = path
            for key in ("assembly_identity", "assembly_version", "requested_execution_level", "manifest_kind"):
                if entry.get(key):
                    project_entry[key] = entry[key]


def _new_project_node(entry: dict) -> dict:
    path = entry["path"]
    return {
        "id": path,
        "kind": "project",
        "name": entry.get("assembly_name") or PurePosixPath(path).stem,
        "path": path,
        "project_kind": entry.get("project_kind") or _default_project_kind(path),
    }


def _refresh_solution_project_counts(node_map: dict[str, dict], edges: list[dict]):
    membership: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.get("kind") == "solution-project" and edge.get("from") and edge.get("to"):
            membership[edge["from"]].add(edge["to"])
    for node in node_map.values():
        if node.get("kind") == "solution":
            node["project_count"] = len(membership.get(node["id"], set()))


def _infer_solution_startup(node_map: dict[str, dict], edges: list[dict]):
    membership: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge.get("kind") == "solution-project" and edge.get("from") and edge.get("to"):
            membership[edge["from"]].append(edge["to"])

    for solution_id, members in membership.items():
        scored: list[tuple[int, str, str]] = []
        for project_id in members:
            project = node_map.get(project_id, {})
            if project.get("kind") != "project":
                continue

            score = 0
            reasons: list[str] = []
            output_type = str(project.get("output_type", "")).lower()
            if output_type in {"exe", "winexe"}:
                score += 4
                reasons.append(f"output_type={project.get('output_type')}")
            if project.get("project_role") == "packaging":
                score -= 3
                reasons.append("packaging-project")
            if project.get("desktop_targets"):
                score += 3
                reasons.append(f"desktop={','.join(project['desktop_targets'])}")
            if project.get("app_xaml"):
                score += 3
                reasons.append("app-xaml")
            if project.get("package_entry_point"):
                score += 2
                reasons.append("package-entry-point")
            if score > 0:
                scored.append((score, project_id, ", ".join(reasons)))

        if not scored:
            continue

        scored.sort(key=lambda item: (-item[0], item[1]))
        best_score = scored[0][0]
        best = [item for item in scored if item[0] == best_score]
        solution_node = node_map.get(solution_id)
        if not solution_node or solution_node.get("kind") != "solution":
            continue

        if len(best) == 1:
            solution_node["startup_project"] = best[0][1]
            solution_node["startup_inference"] = best[0][2]
            if best[0][1] in node_map:
                node_map[best[0][1]]["startup"] = True
        else:
            solution_node["startup_candidates"] = [item[1] for item in best]


def _normalize_rel_path(path: str) -> str:
    return str(path).replace("\\", "/").strip()


def _canonical_project_asset_path(project_path: str, raw_path: str) -> str:
    normalized = _normalize_rel_path(raw_path)
    if not normalized:
        return ""
    pure = PurePosixPath(normalized)
    if pure.is_absolute():
        return normalized
    project_dir = PurePosixPath(project_path).parent
    return posixpath.normpath(_normalize_rel_path((project_dir / pure).as_posix()))
