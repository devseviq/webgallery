from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

from task_runtime.state import coerce_int

from .inventory import SHELL_NAMES, entry_project_memberships, is_xaml_resource_entry


def synthesize_ui_surfaces(files: list[dict], project_graph: dict) -> list[dict]:
    by_project = _files_by_project(files)
    project_nodes = _project_nodes(project_graph)
    startup_projects = _startup_projects(project_graph)
    surfaces: list[dict] = []

    for project_path, project_files in by_project.items():
        project_node = project_nodes.get(project_path, {})
        project_name = project_node.get("name") or PurePosixPath(project_path).stem
        project_is_startup = project_path in startup_projects

        xaml_files = [item for item in project_files if item["path"].lower().endswith((".xaml", ".axaml"))]
        xaml_by_name = {PurePosixPath(item["path"]).name.lower(): item for item in xaml_files}

        app_xaml = xaml_by_name.get("app.xaml") or xaml_by_name.get("app.axaml")
        if app_xaml:
            surfaces.append(
                _surface_record(
                    kind="startup",
                    project=project_path,
                    project_name=project_name,
                    files=[project_path, app_xaml["path"], app_xaml.get("code_behind", "")],
                    entry=app_xaml["path"],
                    startup=project_is_startup,
                )
            )

        for shell_name in sorted(SHELL_NAMES):
            shell_entry = xaml_by_name.get(shell_name)
            if shell_entry:
                surfaces.append(
                    _surface_record(
                        kind="shell",
                        project=project_path,
                        project_name=project_name,
                        files=[project_path, shell_entry["path"], shell_entry.get("code_behind", "")],
                        entry=shell_entry["path"],
                        startup=project_is_startup,
                    )
                )

        resource_files = [item["path"] for item in project_files if is_xaml_resource_entry(item)]
        if resource_files:
            surfaces.append(
                _surface_record(
                    kind="resources",
                    project=project_path,
                    project_name=project_name,
                    files=[project_path, *resource_files],
                    entry=resource_files[0],
                    startup=project_is_startup,
                )
            )

        razor_files = [item for item in project_files if item["path"].lower().endswith(".razor")]
        if razor_files:
            webui_files = [project_path]
            for item in sorted(razor_files, key=lambda value: value["path"]):
                webui_files.append(item["path"])
                if item.get("code_behind"):
                    webui_files.append(item["code_behind"])
            surfaces.append(
                _surface_record(
                    kind="webui",
                    project=project_path,
                    project_name=project_name,
                    files=webui_files,
                    entry=sorted(item["path"] for item in razor_files)[0],
                    startup=project_is_startup,
                )
            )

        package_manifests = [item["path"] for item in project_files if item["path"].lower().endswith(".appxmanifest")]
        if package_manifests:
            surfaces.append(
                _surface_record(
                    kind="packaging",
                    project=project_path,
                    project_name=project_name,
                    files=[project_path, *package_manifests],
                    entry=package_manifests[0],
                    startup=project_is_startup,
                )
            )

        process_manifests = [item["path"] for item in project_files if item["path"].lower().endswith(".manifest")]
        if process_manifests:
            surfaces.append(
                _surface_record(
                    kind="process-manifest",
                    project=project_path,
                    project_name=project_name,
                    files=[project_path, *process_manifests],
                    entry=process_manifests[0],
                    startup=project_is_startup,
                )
            )

    return sorted(surfaces, key=lambda item: (item["project"], item["kind"], item.get("entry", "")))


def synthesize_ownership_summary(files: list[dict], project_graph: dict, ui_surfaces: list[dict]) -> dict:
    project_nodes = _project_nodes(project_graph)
    startup_projects = _startup_projects(project_graph)
    by_project = _files_by_project(files)
    surfaces_by_project: dict[str, list[dict]] = defaultdict(list)
    for surface in ui_surfaces:
        surfaces_by_project[surface["project"]].append(surface)

    project_ids = list(project_nodes.keys()) or sorted(by_project.keys())
    projects: list[dict] = []
    assigned_entries = [entry for entry in files if entry_project_memberships(entry)]
    assigned_files = len(assigned_entries)
    assigned_lines = sum(coerce_int(entry.get("lines", 0) or 0) for entry in assigned_entries)

    for project_id in sorted(project_ids):
        project_files = by_project.get(project_id, [])
        line_count = sum(coerce_int(entry.get("lines", 0) or 0) for entry in project_files)

        package_refs = _package_reference_count(project_nodes.get(project_id, {}), project_files)
        projects.append(
            {
                "project": project_id,
                "name": project_nodes.get(project_id, {}).get("name") or PurePosixPath(project_id).stem,
                "startup": project_id in startup_projects,
                "file_count": len(project_files),
                "line_count": line_count,
                "xaml_file_count": sum(1 for entry in project_files if entry["path"].lower().endswith((".xaml", ".axaml"))),
                "resource_file_count": sum(1 for entry in project_files if is_xaml_resource_entry(entry)),
                "code_behind_file_count": sum(
                    1 for entry in project_files if entry["path"].lower().endswith((".xaml.cs", ".axaml.cs", ".razor.cs"))
                ),
                "package_reference_count": package_refs,
                "ui_surface_count": len(surfaces_by_project.get(project_id, [])),
                "files": sorted(entry["path"] for entry in project_files),
            }
        )

    unassigned_files = [entry for entry in files if not entry_project_memberships(entry)]
    return {
        "project_count": len(projects),
        "assigned_file_count": assigned_files,
        "assigned_line_count": assigned_lines,
        "unassigned_file_count": len(unassigned_files),
        "unassigned_paths": sorted(entry["path"] for entry in unassigned_files),
        "projects": projects,
    }


def _surface_record(*, kind: str, project: str, project_name: str, files: list[str], entry: str, startup: bool) -> dict:
    return {
        "kind": kind,
        "project": project,
        "project_name": project_name,
        "entry": entry,
        "files": sorted({file_path for file_path in files if file_path}),
        "startup": startup,
    }


def _project_nodes(project_graph: dict) -> dict[str, dict]:
    return {node["id"]: node for node in project_graph.get("nodes", []) if node.get("kind") == "project" and node.get("id")}


def _startup_projects(project_graph: dict) -> set[str]:
    startups = {node["id"] for node in project_graph.get("nodes", []) if node.get("kind") == "project" and node.get("startup")}
    for node in project_graph.get("nodes", []):
        if node.get("kind") == "solution" and node.get("startup_project"):
            startups.add(node["startup_project"])
    return startups


def _files_by_project(files: list[dict]) -> dict[str, list[dict]]:
    by_project: dict[str, list[dict]] = defaultdict(list)
    for entry in files:
        for project_path in entry_project_memberships(entry):
            by_project[project_path].append(entry)
    return by_project


def _package_reference_count(project_node: dict, project_files: list[dict]) -> int:
    refs = []
    if isinstance(project_node.get("package_references"), list):
        refs.extend(project_node["package_references"])
    for entry in project_files:
        if isinstance(entry.get("package_references"), list):
            refs.extend(entry["package_references"])
    names = {ref.get("name", "") for ref in refs if isinstance(ref, dict) and ref.get("name")}
    return len(names)
