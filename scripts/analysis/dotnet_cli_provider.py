from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from .inventory import entry_project_memberships, is_database_project_path, set_entry_project_memberships
from .models import AnalysisRequest

DOTNET_CLI_PROVIDER_META = {
    "name": "dotnet-cli",
    "kind": "tool-assisted",
    "implementation": "dotnet-msbuild",
    "confidence": "high",
}

DOTNET_CLI_TIMEOUT_SECONDS = 20

_PROPERTY_NAMES = [
    "TargetFramework",
    "TargetFrameworks",
    "OutputType",
    "UseWPF",
    "UseWinUI",
    "UseBlazorWebView",
    "UseMaui",
    "AssemblyName",
    "RootNamespace",
    "ApplicationManifest",
    "AppxManifest",
    "WindowsPackageType",
    "EnableMsixTooling",
]

_ITEM_NAMES = [
    "ProjectReference",
    "PackageReference",
    "Compile",
    "ClCompile",
    "ClInclude",
    "Page",
    "ApplicationDefinition",
    "None",
]


def dotnet_cli_available(request: AnalysisRequest, current: dict | None = None) -> tuple[bool, str]:
    if shutil.which("dotnet") is None:
        return False, "dotnet-not-found"

    candidates = _candidate_project_paths(request, current)
    if not candidates["projects"] and not candidates["solutions"]:
        return False, "no-dotnet-projects"

    return True, ""


def run_dotnet_cli_analysis(request: AnalysisRequest, current: dict | None = None) -> dict:
    root = request.root
    candidates = _candidate_project_paths(request, current)

    file_updates: dict[str, dict] = {}
    dependency_edges: list[dict] = []
    project_graph_nodes: list[dict] = []
    project_graph_edges: list[dict] = []
    package_graph_nodes: dict[str, dict] = {}
    contribution_count = 0
    failure_reasons: list[str] = []

    solution_discovered: list[str] = []
    for solution_path in candidates["solutions"]:
        try:
            projects = _run_solution_list(root, solution_path)
        except subprocess.TimeoutExpired:
            failure_reasons.append(f"dotnet-cli timed out while listing solution {solution_path}")
            continue
        except subprocess.CalledProcessError:
            failure_reasons.append(f"dotnet-cli failed while listing solution {solution_path}")
            continue
        node = {
            "id": solution_path,
            "kind": "solution",
            "name": Path(solution_path).stem,
            "path": solution_path,
            "project_count": len(projects),
        }
        project_graph_nodes.append(node)
        contribution_count += 1
        for project_path in projects:
            project_graph_edges.append({"from": solution_path, "to": project_path, "kind": "solution-project"})
            if project_path not in candidates["projects"]:
                solution_discovered.append(project_path)

    all_projects = list(dict.fromkeys(candidates["projects"] + solution_discovered))
    for project_path in all_projects:
        try:
            properties = _run_msbuild_query(root, project_path, get_properties=_PROPERTY_NAMES).get("Properties", {})
            items = _run_msbuild_query(root, project_path, get_items=_ITEM_NAMES).get("Items", {})
        except subprocess.TimeoutExpired:
            failure_reasons.append(f"dotnet-cli timed out while analyzing project {project_path}")
            continue
        except (json.JSONDecodeError, OSError, subprocess.CalledProcessError):
            failure_reasons.append(f"dotnet-cli failed while analyzing project {project_path}")
            continue

        project_entry = _project_entry_from_properties(project_path, properties)
        project_entry["project_memberships"] = [project_path]
        file_updates[project_path] = _merge_records(file_updates.get(project_path, {"path": project_path}), project_entry)
        contribution_count += 1

        project_refs = []
        for item in items.get("ProjectReference", []):
            target = _relative_from_msbuild_item(root, project_path, item)
            if not target:
                continue
            project_refs.append(target)
            dependency_edges.append({"from": project_path, "to": target, "kind": "project-reference"})
            project_graph_edges.append({"from": project_path, "to": target, "kind": "project-reference"})

        if project_refs:
            file_updates[project_path]["project_references"] = _merge_lists(
                file_updates[project_path].get("project_references", []),
                project_refs,
            )

        package_references = []
        for item in items.get("PackageReference", []):
            package_ref = _package_reference_from_item(item)
            if not package_ref:
                continue
            package_references.append(package_ref)
            package_node_id = f"nuget:{package_ref['name']}"
            package_node = {
                "id": package_node_id,
                "kind": "package",
                "name": package_ref["name"],
            }
            if package_ref.get("version"):
                package_node["version"] = package_ref["version"]
            package_graph_nodes[package_node_id] = package_node
            project_graph_edges.append({"from": project_path, "to": package_node_id, "kind": "package-reference"})

        if package_references:
            file_updates[project_path]["package_references"] = _merge_package_references(
                file_updates[project_path].get("package_references", []),
                package_references,
            )
            package_targets = _desktop_targets_from_package_references(package_references)
            if package_targets:
                file_updates[project_path]["desktop_targets"] = _merge_lists(
                    file_updates[project_path].get("desktop_targets", []),
                    package_targets,
                )

        for item_name, project_item_kind in (
            ("Compile", "compile"),
            ("ClCompile", "compile"),
            ("ClInclude", "include"),
            ("Page", "page"),
            ("ApplicationDefinition", "application-definition"),
            ("None", "none"),
        ):
            for item in _dedupe_msbuild_items(items.get(item_name, [])):
                rel_path = _relative_from_msbuild_item(root, project_path, item)
                if not rel_path:
                    continue
                update: dict[str, object] = {"path": rel_path, "project": project_path, "project_memberships": [project_path]}
                file_path = root / rel_path
                if file_path.is_file():
                    try:
                        update["lines"] = sum(1 for _ in file_path.open(encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
                if project_item_kind != "compile":
                    update["project_item_kind"] = project_item_kind
                if item.get("Generator"):
                    update["project_item_generator"] = item["Generator"]
                if item.get("XamlRuntime"):
                    update["xaml_runtime"] = item["XamlRuntime"]
                if item.get("SubType"):
                    update["project_item_subtype"] = item["SubType"]
                if item.get("Link"):
                    update["project_item_link"] = str(item["Link"]).replace("\\", "/")
                if item.get("DependentUpon"):
                    update["dependent_upon"] = item["DependentUpon"]
                    dependent_path = _resolve_dependent_upon(rel_path, item["DependentUpon"])
                    if dependent_path:
                        update["code_behind"] = dependent_path
                file_updates[rel_path] = _merge_records(file_updates.get(rel_path, {"path": rel_path}), update)

        project_graph_nodes.append(_project_node_from_entry(project_path, file_updates[project_path]))

    if contribution_count == 0:
        raise RuntimeError(failure_reasons[-1] if failure_reasons else "no successful dotnet-cli analysis")

    return {
        "provider": DOTNET_CLI_PROVIDER_META,
        "inventory": {
            "files": list(file_updates.values()),
        },
        "graphs": {
            "dependency_edges": dependency_edges,
            "project_graph": {
                "nodes": [*project_graph_nodes, *package_graph_nodes.values()],
                "edges": project_graph_edges,
            },
        },
        "signals": {
            "conflict_zones": [],
        },
    }


def _candidate_project_paths(request: AnalysisRequest, current: dict | None = None) -> dict[str, list[str]]:
    root = request.root
    inventory = ((current or {}).get("inventory") or {}).get("files", [])
    rel_paths = [entry.get("path", "") for entry in inventory if entry.get("path")]

    projects = sorted(
        {rel_path for rel_path in rel_paths if rel_path.lower().endswith((".csproj", ".wapproj", ".vcxproj", ".sqlproj"))}
        or {
            str(path.relative_to(root)).replace("\\", "/")
            for pattern in ("*.csproj", "*.wapproj", "*.vcxproj", "*.sqlproj")
            for path in root.rglob(pattern)
            if path.is_file()
        }
    )
    solutions = sorted(
        {rel_path for rel_path in rel_paths if rel_path.lower().endswith((".sln", ".slnx"))}
        or {
            str(path.relative_to(root)).replace("\\", "/")
            for pattern in ("*.sln", "*.slnx")
            for path in root.rglob(pattern)
            if path.is_file()
        }
    )
    return {"projects": projects, "solutions": solutions}


def _run_solution_list(root: Path, solution_path: str) -> list[str]:
    command = ["dotnet", "sln", str(root / solution_path), "list"]
    result = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOTNET_CLI_TIMEOUT_SECONDS,
    )
    projects: list[str] = []
    solution_dir = (root / solution_path).resolve().parent
    for line in result.stdout.splitlines():
        text = line.strip()
        if not text or text.startswith("Project(s)") or text.startswith("Description:") or text.startswith("Usage:"):
            continue
        if text.endswith((".csproj", ".wapproj", ".vcxproj", ".sqlproj")):
            candidate = (solution_dir / text.replace("\\", "/")).resolve()
            try:
                projects.append(str(candidate.relative_to(root.resolve())).replace("\\", "/"))
            except ValueError:
                continue
    return projects


def _run_msbuild_query(
    root: Path,
    project_path: str,
    *,
    get_properties: list[str] | None = None,
    get_items: list[str] | None = None,
) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        output_file = Path(tmp.name)

    command = [
        "dotnet",
        "msbuild",
        str(root / project_path),
        "-nologo",
        "-verbosity:quiet",
        "-tl:off",
        f"-getResultOutputFile:{output_file}",
    ]
    if get_properties:
        command.append(f"-getProperty:{','.join(get_properties)}")
    if get_items:
        command.append(f"-getItem:{','.join(get_items)}")

    try:
        subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=DOTNET_CLI_TIMEOUT_SECONDS,
        )
        return json.loads(output_file.read_text(encoding="utf-8"))
    finally:
        output_file.unlink(missing_ok=True)


def _relative_from_msbuild_item(root: Path, project_path: str, item: dict) -> str:
    full_path = str(item.get("FullPath", "")).strip()
    if full_path:
        try:
            return str(Path(full_path).resolve().relative_to(root.resolve())).replace("\\", "/")
        except ValueError:
            return ""

    identity = str(item.get("Identity", "")).strip()
    if not identity:
        return ""
    candidate = ((root / project_path).resolve().parent / identity).resolve()
    try:
        return str(candidate.relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        return ""


def _project_entry_from_properties(project_path: str, properties: dict) -> dict:
    entry: dict = {"path": project_path}

    target_frameworks: list[str] = []
    for key in ("TargetFramework", "TargetFrameworks"):
        raw = str(properties.get(key, "")).strip()
        if raw:
            target_frameworks.extend(part.strip() for part in raw.split(";") if part.strip())
    if target_frameworks:
        entry["target_frameworks"] = sorted(set(target_frameworks))

    desktop_targets = []
    if str(properties.get("UseWPF", "")).strip().lower() == "true":
        desktop_targets.append("wpf")
    if str(properties.get("UseWinUI", "")).strip().lower() == "true":
        desktop_targets.append("winui")
    if str(properties.get("UseBlazorWebView", "")).strip().lower() == "true":
        desktop_targets.append("blazor-hybrid")
    if str(properties.get("UseMaui", "")).strip().lower() == "true":
        desktop_targets.append("maui")
    if desktop_targets:
        entry["desktop_targets"] = sorted(set(desktop_targets))

    if str(properties.get("OutputType", "")).strip():
        entry["output_type"] = str(properties["OutputType"]).strip()
    if str(properties.get("AssemblyName", "")).strip():
        entry["assembly_name"] = str(properties["AssemblyName"]).strip()
    if str(properties.get("RootNamespace", "")).strip():
        entry["root_namespace"] = str(properties["RootNamespace"]).strip()
    if str(properties.get("ApplicationManifest", "")).strip():
        entry["application_manifest"] = str(properties["ApplicationManifest"]).strip()
    if str(properties.get("AppxManifest", "")).strip():
        entry["appx_manifest"] = str(properties["AppxManifest"]).strip()
    if str(properties.get("WindowsPackageType", "")).strip():
        entry["windows_package_type"] = str(properties["WindowsPackageType"]).strip()
    if str(properties.get("EnableMsixTooling", "")).strip().lower() == "true":
        entry["packaging_model"] = "msix"
    if project_path.lower().endswith(".wapproj"):
        entry["project_role"] = "packaging"
        entry.setdefault("packaging_model", "msix")
    if is_database_project_path(project_path):
        entry["project_role"] = "database"
        entry["project_kind"] = "database"

    return entry


def _project_node_from_entry(project_path: str, entry: dict) -> dict:
    node = {
        "id": project_path,
        "kind": "project",
        "name": entry.get("assembly_name") or Path(project_path).stem,
        "path": project_path,
        "project_kind": entry.get("project_kind") or ("database" if is_database_project_path(project_path) else "msbuild"),
    }
    for key in (
        "desktop_targets",
        "target_frameworks",
        "output_type",
        "project_role",
        "packaging_model",
        "app_xaml",
        "package_manifest",
        "package_identity",
        "package_entry_point",
        "package_references",
    ):
        if entry.get(key):
            node[key] = entry[key]
    return node


def _package_reference_from_item(item: dict) -> dict:
    name = str(item.get("Identity", "") or item.get("Include", "")).strip()
    if not name:
        return {}

    package_ref = {"name": name}
    if str(item.get("Version", "")).strip():
        package_ref["version"] = str(item["Version"]).strip()
    if str(item.get("PrivateAssets", "")).strip():
        package_ref["private_assets"] = str(item["PrivateAssets"]).strip()
    if str(item.get("IncludeAssets", "")).strip():
        package_ref["include_assets"] = str(item["IncludeAssets"]).strip()
    if str(item.get("ExcludeAssets", "")).strip():
        package_ref["exclude_assets"] = str(item["ExcludeAssets"]).strip()
    return package_ref


def _merge_package_references(existing: list[dict], incoming: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {entry.get("name", ""): dict(entry) for entry in existing if entry.get("name")}
    order = [entry.get("name", "") for entry in existing if entry.get("name")]
    for package_ref in incoming:
        name = package_ref.get("name", "")
        if not name:
            continue
        if name not in merged:
            merged[name] = dict(package_ref)
            order.append(name)
            continue
        merged[name] = _merge_records(merged[name], package_ref)
    return [merged[name] for name in order]


def _dedupe_msbuild_items(items: list[dict]) -> list[dict]:
    ranked: dict[str, dict] = {}
    order: list[str] = []
    for item in items:
        key = str(item.get("FullPath", "")).strip() or str(item.get("Identity", "")).strip()
        if not key:
            continue
        if key not in ranked:
            ranked[key] = item
            order.append(key)
            continue
        if _item_priority(item) >= _item_priority(ranked[key]):
            ranked[key] = item
    return [ranked[key] for key in order]


def _item_priority(item: dict) -> int:
    defining_extension = str(item.get("DefiningProjectExtension", "")).lower()
    if defining_extension in {".csproj", ".wapproj", ".sqlproj", ".props", ".targets"}:
        if defining_extension in {".csproj", ".wapproj", ".sqlproj"}:
            return 3
        return 2
    return 1


def _resolve_dependent_upon(rel_path: str, dependent_upon: str) -> str:
    rel = Path(rel_path)
    candidate = rel.parent / str(dependent_upon).replace("\\", "/")
    return candidate.as_posix()


def _merge_records(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)
    merged_projects = _merge_lists(
        entry_project_memberships(existing),
        entry_project_memberships(incoming),
    )
    for key, value in incoming.items():
        if key in {"project", "project_memberships"}:
            continue
        if key == "path":
            merged[key] = value
            continue
        if isinstance(merged.get(key), list) and isinstance(value, list):
            merged[key] = _merge_lists(merged[key], value)
            continue
        if isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _merge_records(merged[key], value)
            continue
        if value not in ("", None, [], {}):
            merged[key] = value
    if merged_projects:
        set_entry_project_memberships(merged, merged_projects)
    return merged


def _merge_lists(existing: list, incoming: list) -> list:
    merged = list(existing)
    for item in incoming:
        if item not in merged:
            merged.append(item)
    return merged


def _desktop_targets_from_package_references(package_references: list[dict]) -> list[str]:
    targets: list[str] = []
    for package_ref in package_references:
        name = str(package_ref.get("name", "")).strip().lower()
        if name.startswith("avalonia") and "avalonia" not in targets:
            targets.append("avalonia")
    return targets
