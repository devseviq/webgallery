from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path, PurePosixPath

from task_runtime import (
    get_conflict_zones as _get_conflict_zones,
    get_first_party as _get_first_party,
    get_module_map as _get_module_map,
)

from .inventory import (
    SHELL_NAMES,
    entry_project_memberships,
    is_database_file,
    is_xaml_resource_entry,
    looks_like_database_migration,
    set_entry_project_memberships,
)
from .models import AnalysisRequest

BASIC_PROVIDER_META = {
    "name": "basic",
    "kind": "heuristic",
    "implementation": "python-stdlib",
    "confidence": "medium",
}

DEFAULT_ANALYSIS_INCLUDE_GLOBS = [
    "*.py",
    "*.ps1",
    "*.html",
    "*.css",
    "*.scss",
    "*.js",
    "*.jsx",
    "*.ts",
    "*.tsx",
    "*.md",
    "*.json",
    "*.sql",
    "*.toml",
    "*.yml",
    "*.yaml",
    "*.cs",
    "*.csproj",
    "*.sqlproj",
    "*.sln",
    "*.slnx",
    "*.props",
    "*.targets",
    "*.wapproj",
    "*.razor",
    "*.xaml",
    "*.axaml",
    "*.appxmanifest",
    "*.manifest",
    "*.cpp",
    "*.cxx",
    "*.cc",
    "*.c",
    "*.hpp",
    "*.hxx",
    "*.hh",
    "*.h",
    "*.vcxproj",
    "*.cmake",
    "CMakeLists.txt",
]

DEFAULT_ANALYSIS_EXCLUDE_GLOBS = [
    ".git/**",
    ".mypy_cache/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".tox/**",
    ".venv/**",
    "node_modules/**",
    ".tmp*/**",
    "dist/**",
    "bin/**",
    "obj/**",
    "__pycache__/**",
    "venv/**",
]

_XAML_PARSE_SIZE_LIMIT = 5 * 1024 * 1024
_CPP_SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}
_CPP_HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}


def _normalize_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                items.append(text)
        return items
    text = str(value).strip()
    return [text] if text else []


def _analysis_cfg(cfg: dict) -> dict:
    return cfg.get("analysis", {})


def _analysis_include_globs(cfg: dict) -> list[str]:
    configured = _normalize_string_list(_analysis_cfg(cfg).get("include-globs", []))
    return configured or list(DEFAULT_ANALYSIS_INCLUDE_GLOBS)


def _analysis_exclude_globs(cfg: dict) -> list[str]:
    configured = _normalize_string_list(_analysis_cfg(cfg).get("exclude-globs", []))
    return configured or list(DEFAULT_ANALYSIS_EXCLUDE_GLOBS)


def _analysis_matches_glob(rel: str, pattern: str) -> bool:
    import fnmatch
    import re as _re

    normalized = rel.replace("\\", "/")
    if pattern.endswith("/"):
        pattern = f"{pattern}**"
    try:
        return fnmatch.fnmatchcase(normalized, pattern)
    except (ValueError, _re.error):
        return False


def _should_skip_analysis_path(rel: str, cfg: dict) -> bool:
    normalized = rel.replace("\\", "/")
    return any(_analysis_matches_glob(normalized, pattern) for pattern in _analysis_exclude_globs(cfg))


def _iter_analysis_files(root: Path, cfg: dict) -> list[Path]:
    seen: set[str] = set()
    discovered: list[Path] = []

    for pattern in _analysis_include_globs(cfg):
        for path in sorted(root.rglob(pattern)):
            if not path.is_file():
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            if rel in seen or _should_skip_analysis_path(rel, cfg):
                continue
            seen.add(rel)
            discovered.append(path)

    return discovered


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for _ in path.open(encoding="utf-8", errors="replace"))
    except (OSError, UnicodeDecodeError):
        return 0


def _classify_file(rel: str, module_map: dict[str, list[str]]) -> str:
    """Assign a file to a module category."""
    for category, prefixes in module_map.items():
        for prefix in prefixes:
            if prefix.endswith("/"):
                if rel.startswith(prefix):
                    return category
            elif rel == prefix:
                return category
    return "other"


def _extract_python_imports(path: Path, first_party: set[str]) -> list[str]:
    """Extract import targets from a Python file (first-party only)."""
    imports: list[str] = []
    try:
        for line in path.open(encoding="utf-8", errors="replace"):
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            from_match = re.match(r"^from\s+([A-Za-z_][\w.]*)\s+import\b", line)
            if from_match:
                import_path = from_match.group(1)
                if import_path.split(".")[0] in first_party:
                    imports.append(import_path)
                continue
            import_match = re.match(r"^import\s+(.+)$", line)
            if not import_match:
                continue
            for raw_import in import_match.group(1).split(","):
                import_path = raw_import.strip().split(" as ", 1)[0].strip()
                if not import_path:
                    continue
                if re.match(r"^[A-Za-z_][\w.]*$", import_path) and import_path.split(".")[0] in first_party:
                    imports.append(import_path)
    except OSError:
        pass
    return sorted(set(imports))


def _extract_python_definitions(path: Path) -> dict:
    """Extract class and function names from a Python file."""
    classes: list[str] = []
    functions: list[str] = []
    try:
        for line in path.open(encoding="utf-8", errors="replace"):
            class_match = re.match(r"^class\s+(\w+)", line)
            if class_match:
                classes.append(class_match.group(1))
            function_match = re.match(r"^def\s+(\w+)", line)
            if function_match:
                functions.append(function_match.group(1))
    except OSError:
        pass
    return {"classes": classes, "functions": functions}


def _extract_csharp_metadata(path: Path) -> dict:
    """Extract lightweight symbols from a C# file."""
    usings: list[str] = []
    namespaces: list[str] = []
    types: list[str] = []
    try:
        for line in path.open(encoding="utf-8", errors="replace"):
            using_match = re.match(r"^\s*using\s+([A-Za-z_][\w.]*)\s*;", line)
            if using_match:
                usings.append(using_match.group(1))
            namespace_match = re.match(r"^\s*namespace\s+([A-Za-z_][\w.]*)", line)
            if namespace_match:
                namespaces.append(namespace_match.group(1))
            type_match = re.search(
                r"\b(?:(?:public|private|protected|internal|file|sealed|abstract|static|partial|readonly|ref|unsafe)\s+)*"
                r"(?:class|interface|struct|enum|record(?:\s+(?:class|struct))?)\s+([A-Za-z_]\w*)\b",
                line,
            )
            if type_match:
                types.append(type_match.group(1))
    except OSError:
        pass
    return {
        "usings": sorted(set(usings)),
        "namespaces": sorted(set(namespaces)),
        "types": sorted(set(types)),
    }


def _extract_razor_metadata(path: Path) -> dict:
    """Extract routes, using directives, and injections from a Razor component."""
    routes: list[str] = []
    usings: list[str] = []
    injects: list[str] = []
    try:
        for line in path.open(encoding="utf-8", errors="replace"):
            page_match = re.match(r'^\s*@page\s+"([^"]+)"', line)
            if page_match:
                route = page_match.group(1).strip()
                if route and route not in routes:
                    routes.append(route)
            using_match = re.match(r"^\s*@using\s+([A-Za-z_][\w.]*)\s*$", line)
            if using_match:
                usings.append(using_match.group(1))
            inject_match = re.match(r"^\s*@inject\s+([A-Za-z_][\w.<>?,\[\]]*)\s+[A-Za-z_]\w*", line)
            if inject_match:
                inject_type = inject_match.group(1).split(".")[-1]
                injects.append(inject_type)
    except OSError:
        return {}

    metadata: dict[str, list[str]] = {}
    if routes:
        metadata["razor_routes"] = routes
    if injects:
        metadata["razor_injects"] = sorted(set(injects))
    if usings:
        metadata["usings"] = sorted(set(usings))
    return metadata


def _extract_csharp_reference_candidates(path: Path) -> list[str]:
    """Extract candidate type references from a C# file using lightweight heuristics."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    text = re.sub(r"//.*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r'@"(?:[^"]|"")*"', " ", text)
    text = re.sub(r'"(?:\\.|[^"\\])*"', " ", text)
    text = re.sub(r"'(?:\\.|[^'\\])+'", " ", text)

    candidates = re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", text)
    noise = {
        "System",
        "String",
        "Boolean",
        "Int32",
        "Int64",
        "Double",
        "Decimal",
        "Object",
        "Task",
        "List",
        "Dictionary",
        "Enumerable",
        "Window",
        "Application",
        "Page",
        "UserControl",
        "FrameworkElement",
        "DependencyObject",
        "ResourceDictionary",
    }
    return sorted({candidate for candidate in candidates if candidate not in noise})


def _extract_cpp_metadata(path: Path) -> dict:
    """Extract local include and type hints from a C/C++ file."""
    includes: list[str] = []
    symbols: list[str] = []
    try:
        for line in path.open(encoding="utf-8", errors="replace"):
            include_match = re.match(r'^\s*#include\s*[<"]([^">]+)[">]', line)
            if include_match:
                includes.append(include_match.group(1))
            symbol_match = re.match(r"^\s*(?:class|struct|namespace|enum(?:\s+class)?)\s+([A-Za-z_]\w*)\b", line)
            if symbol_match:
                symbols.append(symbol_match.group(1))
    except OSError:
        pass
    return {
        "includes": sorted(set(includes)),
        "symbols": sorted(set(symbols)),
    }


def _extract_xaml_metadata(path: Path) -> dict:
    """Extract the root element and x:Class from XAML-like markup."""
    try:
        if path.stat().st_size > _XAML_PARSE_SIZE_LIMIT:
            return {"skipped_reason": "file_too_large"}
    except OSError:
        return {}

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    metadata: dict[str, str | list[str]] = {}
    root_match = re.search(r"<\s*([A-Za-z_][\w.:]*)\b", text)
    if root_match:
        root_element = root_match.group(1)
        metadata["root_element"] = root_element
        metadata["root_element_type"] = root_element.split(":")[-1]
    class_match = re.search(r'\bx:Class\s*=\s*"([^"]+)"', text)
    if class_match:
        metadata["xaml_class"] = class_match.group(1)
    merge_sources: list[str] = []
    for source in re.findall(
        r'<\s*(?:[A-Za-z_][\w.-]*:)?(?:ResourceDictionary|StyleInclude)\b[^>]*\bSource\s*=\s*"([^"]+)"',
        text,
        flags=re.IGNORECASE,
    ):
        normalized = source.strip()
        if normalized and normalized not in merge_sources:
            merge_sources.append(normalized)
    if merge_sources:
        metadata["resource_merge_sources"] = merge_sources
    if path.suffix.lower() == ".axaml" or "https://github.com/avaloniaui" in text:
        metadata["xaml_framework"] = "avalonia"
    return metadata


def _extract_msbuild_project_metadata(path: Path) -> dict:
    """Extract project references and desktop UI markers from MSBuild files."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    refs = re.findall(r'<ProjectReference\s+Include="([^"]+)"', text, re.IGNORECASE)
    targets: list[str] = []
    if re.search(r"<UseWPF>\s*true\s*</UseWPF>", text, re.IGNORECASE):
        targets.append("wpf")
    if re.search(r"(?:<UseWinUI>\s*true\s*</UseWinUI>|Microsoft\.WindowsAppSDK|Microsoft\.UI\.Xaml)", text, re.IGNORECASE):
        targets.append("winui")
    if re.search(r"<UseBlazorWebView>\s*true\s*</UseBlazorWebView>", text, re.IGNORECASE):
        targets.append("blazor-hybrid")
    if re.search(r"<UseMaui>\s*true\s*</UseMaui>", text, re.IGNORECASE):
        targets.append("maui")
    if re.search(r'<PackageReference\s+Include="Avalonia(?:\.[^"]*)?"', text, re.IGNORECASE):
        targets.append("avalonia")
    tfms = re.findall(r"<TargetFrameworks?>\s*([^<]+)\s*</TargetFrameworks?>", text, re.IGNORECASE)
    output_type = re.search(r"<OutputType>\s*([^<]+)\s*</OutputType>", text, re.IGNORECASE)
    assembly_name = re.search(r"<AssemblyName>\s*([^<]+)\s*</AssemblyName>", text, re.IGNORECASE)
    root_namespace = re.search(r"<RootNamespace>\s*([^<]+)\s*</RootNamespace>", text, re.IGNORECASE)
    app_manifest = re.search(r"<ApplicationManifest>\s*([^<]+)\s*</ApplicationManifest>", text, re.IGNORECASE)
    appx_manifest = re.search(r"<AppxManifest>\s*([^<]+)\s*</AppxManifest>", text, re.IGNORECASE)
    windows_package_type = re.search(r"<WindowsPackageType>\s*([^<]+)\s*</WindowsPackageType>", text, re.IGNORECASE)
    enable_msix_tooling = re.search(r"<EnableMsixTooling>\s*true\s*</EnableMsixTooling>", text, re.IGNORECASE)

    metadata: dict[str, object] = {
        "project_references": sorted(set(refs)),
        "desktop_targets": sorted(set(targets)),
        "target_frameworks": sorted({item.strip() for match in tfms for item in match.split(";") if item.strip()}),
    }
    if output_type:
        metadata["output_type"] = output_type.group(1).strip()
    if assembly_name:
        metadata["assembly_name"] = assembly_name.group(1).strip()
    if root_namespace:
        metadata["root_namespace"] = root_namespace.group(1).strip()
    if app_manifest:
        metadata["application_manifest"] = app_manifest.group(1).strip()
    if appx_manifest:
        metadata["appx_manifest"] = appx_manifest.group(1).strip()
    if windows_package_type:
        metadata["windows_package_type"] = windows_package_type.group(1).strip()
    if enable_msix_tooling:
        metadata["packaging_model"] = "msix"
    if path.suffix.lower() == ".wapproj":
        metadata["project_role"] = "packaging"
        metadata.setdefault("packaging_model", "msix")
    if path.suffix.lower() == ".sqlproj":
        metadata["project_role"] = "database"
        metadata["project_kind"] = "database"
    return metadata


def _extract_cmake_metadata(path: Path) -> dict:
    """Extract target declarations from CMake files."""
    targets: list[str] = []
    sources: dict[str, list[str]] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    modifier_tokens = {
        "ALIAS",
        "EXCLUDE_FROM_ALL",
        "IMPORTED",
        "INTERFACE",
        "MODULE",
        "OBJECT",
        "SHARED",
        "STATIC",
        "WIN32",
    }
    for match in re.finditer(r"(?is)\badd_(?:executable|library)\s*\(\s*([^\s)]+)\s*([^)]*)\)", text):
        target = match.group(1).strip()
        if not target:
            continue
        targets.append(target)
        body_tokens = [token.strip('"') for token in re.findall(r'"[^"]+"|\S+', match.group(2))]
        target_sources: list[str] = []
        for token in body_tokens:
            if not token or token == target or token.upper() in modifier_tokens:
                continue
            target_sources.append(token)
        if target_sources:
            sources[target] = target_sources

    metadata: dict[str, object] = {"cmake_targets": sorted(set(targets))}
    if sources:
        metadata["cmake_sources"] = sources
    return metadata


def _extract_solution_metadata(path: Path) -> dict:
    """Extract projects listed in a Visual Studio solution."""
    if path.suffix.lower() == ".slnx":
        return _extract_solutionx_metadata(path)

    projects: list[dict] = []
    try:
        for line in path.open(encoding="utf-8", errors="replace"):
            match = re.match(
                r'^Project\("(?P<type_guid>[^"]+)"\)\s*=\s*"(?P<name>[^"]+)",\s*"(?P<path>[^"]+)",\s*"(?P<guid>[^"]+)"',
                line.strip(),
            )
            if not match:
                continue
            rel_path = match.group("path").replace("\\", "/")
            projects.append(
                {
                    "name": match.group("name").strip(),
                    "path": rel_path,
                    "guid": match.group("guid").strip("{}").lower(),
                    "type_guid": match.group("type_guid").strip("{}").lower(),
                }
            )
    except OSError:
        return {}
    return {"solution_projects": projects}


def _extract_solutionx_metadata(path: Path) -> dict:
    """Extract projects listed in a modern XML solution file."""
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
    except (ET.ParseError, OSError):
        return {}

    projects: list[dict] = []

    def visit(node: ET.Element):
        for child in node:
            local = _xml_local_name(child.tag)
            if local == "Project":
                rel_path = str(child.attrib.get("Path", "")).replace("\\", "/").strip()
                if rel_path:
                    projects.append(
                        {
                            "name": PurePosixPath(rel_path).stem,
                            "path": rel_path,
                            "guid": "",
                            "type_guid": "",
                        }
                    )
            visit(child)

    visit(root)
    return {"solution_projects": projects}


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _extract_manifest_metadata(path: Path) -> dict:
    """Extract lightweight metadata from app package manifests."""
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
    except (ET.ParseError, OSError):
        return {}

    metadata: dict[str, str] = {}
    suffix = path.suffix.lower()
    if suffix == ".appxmanifest":
        metadata["manifest_kind"] = "appx"
        for node in root.iter():
            local = _xml_local_name(node.tag)
            if local == "Identity":
                if node.attrib.get("Name"):
                    metadata["package_identity"] = node.attrib["Name"]
                if node.attrib.get("Publisher"):
                    metadata["package_publisher"] = node.attrib["Publisher"]
                if node.attrib.get("Version"):
                    metadata["package_version"] = node.attrib["Version"]
            elif local == "DisplayName" and not metadata.get("package_display_name"):
                text = (node.text or "").strip()
                if text:
                    metadata["package_display_name"] = text
            elif local == "Application" and not metadata.get("package_entry_point"):
                if node.attrib.get("Executable"):
                    metadata["package_executable"] = node.attrib["Executable"]
                if node.attrib.get("EntryPoint"):
                    metadata["package_entry_point"] = node.attrib["EntryPoint"]
                if node.attrib.get("Id"):
                    metadata["package_application_id"] = node.attrib["Id"]
    elif suffix == ".manifest":
        metadata["manifest_kind"] = "windows-application"
        for node in root.iter():
            local = _xml_local_name(node.tag)
            if local == "assemblyIdentity":
                if node.attrib.get("name"):
                    metadata["assembly_identity"] = node.attrib["name"]
                if node.attrib.get("version"):
                    metadata["assembly_version"] = node.attrib["version"]
            elif local == "requestedExecutionLevel" and node.attrib.get("level"):
                metadata["requested_execution_level"] = node.attrib["level"]

    return metadata


def _resolve_analysis_reference(source_rel: str, target_ref: str, known_paths: set[str], name_index: dict[str, list[str]]) -> str:
    normalized_target = target_ref.replace("\\", "/")
    candidate = posixpath.normpath((PurePosixPath(source_rel).parent / normalized_target).as_posix())
    if candidate in known_paths:
        return candidate
    target_name = PurePosixPath(normalized_target).name
    matches = name_index.get(target_name, [])
    if len(matches) == 1:
        return matches[0]
    return ""


def _resolve_python_import_reference(import_path: str, known_paths: set[str]) -> str:
    normalized = import_path.replace(".", "/").strip("/")
    if not normalized:
        return ""

    for candidate in (f"{normalized}.py", f"{normalized}/__init__.py"):
        if candidate in known_paths:
            return candidate
    return ""


def _add_dependency_edge(edges: list[dict], seen: set[tuple[str, str, str]], source: str, target: str, kind: str):
    if not source or not target or source == target:
        return
    key = (source, target, kind)
    if key in seen:
        return
    seen.add(key)
    edge = {"from": source, "to": target}
    if kind:
        edge["kind"] = kind
    edges.append(edge)


def _summarize_detected_stacks(files: list[dict]) -> list[str]:
    stacks: set[str] = set()
    for entry in files:
        path_lower = entry["path"].lower()
        name_lower = PurePosixPath(entry["path"]).name.lower()

        if path_lower.endswith((".cs", ".csproj", ".sln", ".slnx", ".props", ".targets")):
            stacks.add("dotnet")
        if path_lower.endswith(".razor"):
            stacks.add("blazor")
        if path_lower.endswith(".xaml") or path_lower.endswith(".axaml"):
            stacks.add("xaml-ui")
        if path_lower.endswith(".axaml") or entry.get("xaml_framework") == "avalonia":
            stacks.add("avalonia")
        if path_lower.endswith((".sql", ".sqlproj")):
            stacks.add("database")
        if path_lower.endswith(".appxmanifest") or entry.get("packaging_model") == "msix":
            stacks.add("msix")
        if path_lower.endswith((".cpp", ".cxx", ".cc", ".c", ".hpp", ".hxx", ".hh", ".h", ".vcxproj")) or name_lower == "cmakelists.txt":
            stacks.add("cpp")
        for target in entry.get("desktop_targets", []):
            stacks.add(target)
            if target == "blazor-hybrid":
                stacks.add("blazor")
            if target == "avalonia":
                stacks.add("avalonia")

    return sorted(stacks)


def _entry_project_root(entry: dict) -> str:
    return PurePosixPath(entry["path"]).parent.as_posix()


def _build_project_index(files: list[dict]) -> dict[str, dict]:
    projects: dict[str, dict] = {}
    for entry in files:
        path = entry["path"]
        suffix = PurePosixPath(path).suffix.lower()
        name = PurePosixPath(path).name
        if suffix not in {".csproj", ".vcxproj", ".wapproj", ".sqlproj"} and name != "CMakeLists.txt":
            continue
        projects[path] = {
            "path": path,
            "root": _entry_project_root(entry),
            "name": entry.get("assembly_name") or PurePosixPath(path).stem or PurePosixPath(path).parent.name,
            "kind": "cmake" if name == "CMakeLists.txt" else entry.get("project_kind", "database" if suffix == ".sqlproj" else "msbuild"),
            "desktop_targets": list(entry.get("desktop_targets", [])),
            "target_frameworks": list(entry.get("target_frameworks", [])),
            "output_type": entry.get("output_type", ""),
            "project_role": entry.get("project_role", "database" if suffix == ".sqlproj" else "application"),
            "packaging_model": entry.get("packaging_model", ""),
        }
        for key in ("root_namespace", "application_manifest", "appx_manifest", "windows_package_type"):
            if entry.get(key):
                projects[path][key] = entry[key]
    return projects


def _assign_files_to_projects(files: list[dict], projects: dict[str, dict]):
    if not projects:
        return

    candidates: list[tuple[str, str]] = []
    for project_path, project in projects.items():
        root = project.get("root", ".")
        prefix = "" if root in ("", ".") else f"{root}/"
        candidates.append((project_path, prefix))

    candidates.sort(key=lambda item: len(item[1]), reverse=True)

    for entry in files:
        path = entry["path"]
        if path in projects:
            set_entry_project_memberships(entry, [path])
            continue
        matched_candidates = [projects[project_path] for project_path, prefix in candidates if not prefix or path.startswith(prefix)]
        if not matched_candidates:
            continue

        max_prefix_len = max(len(project["root"]) for project in matched_candidates)
        scoped_candidates = [project for project in matched_candidates if len(project["root"]) == max_prefix_len]
        if len(scoped_candidates) == 1:
            set_entry_project_memberships(entry, [scoped_candidates[0]["path"]])
            continue

        selected = _select_project_candidate(entry, scoped_candidates)
        if selected:
            set_entry_project_memberships(entry, [selected["path"]])


def _select_project_candidate(entry: dict, candidates: list[dict]) -> dict | None:
    path_lower = entry["path"].lower()
    ui_related = path_lower.endswith((".xaml", ".axaml", ".xaml.cs", ".axaml.cs")) or is_xaml_resource_entry(entry)
    database_related = path_lower.endswith(".sql")
    packaging_related = path_lower.endswith((".appxmanifest", ".manifest"))

    if packaging_related:
        packaging = [project for project in candidates if project.get("project_role") == "packaging"]
        if len(packaging) == 1:
            return packaging[0]

    if database_related:
        database = [project for project in candidates if project.get("project_role") == "database"]
        if len(database) == 1:
            return database[0]

    if ui_related:
        desktop = [project for project in candidates if project.get("desktop_targets")]
        desktop_non_test = [project for project in desktop if not _looks_like_test_project(project)]
        executable_desktop = [project for project in desktop_non_test if str(project.get("output_type", "")).lower() in {"exe", "winexe"}]
        if len(executable_desktop) == 1:
            return executable_desktop[0]
        if len(desktop_non_test) == 1:
            return desktop_non_test[0]
        if len(desktop) == 1:
            return desktop[0]

    non_packaging = [project for project in candidates if project.get("project_role") != "packaging"]
    non_test = [project for project in non_packaging if not _looks_like_test_project(project)]
    if len(non_test) == 1:
        return non_test[0]
    if len(non_packaging) == 1:
        return non_packaging[0]
    return None


def _looks_like_test_project(project: dict) -> bool:
    candidates = [
        str(project.get("path", "")),
        str(project.get("name", "")),
        str(project.get("root_namespace", "")),
    ]
    for value in candidates:
        if re.search(r"(^|[._-])(test|tests|spec|specs)($|[._-])", value, re.IGNORECASE):
            return True
    return False


def _append_conflict_zone(zones: list[dict], files: list[str], reason: str):
    normalized_files = sorted({file_path for file_path in files if file_path})
    if len(normalized_files) < 2:
        return
    zone = {"files": normalized_files, "reason": reason}
    if zone not in zones:
        zones.append(zone)


def _build_csharp_type_index(files: list[dict]) -> dict[str, list[dict]]:
    type_index: dict[str, list[dict]] = defaultdict(list)
    for entry in files:
        if not entry["path"].lower().endswith(".cs"):
            continue
        namespaces = entry.get("namespaces", []) or [""]
        for type_name in entry.get("types", []):
            record = {
                "path": entry["path"],
                "project": entry.get("project", ""),
                "type": type_name,
                "full_names": [f"{namespace}.{type_name}" if namespace else type_name for namespace in namespaces],
            }
            type_index[type_name].append(record)
    return type_index


def _resolve_csharp_type_reference(source_entry: dict, type_name: str, type_index: dict[str, list[dict]]) -> dict | None:
    candidates = [candidate for candidate in type_index.get(type_name, []) if candidate["path"] != source_entry["path"]]
    if not candidates:
        return None

    source_namespaces = {f"{namespace}.{type_name}" for namespace in source_entry.get("namespaces", []) if namespace}
    same_namespace = [
        candidate for candidate in candidates if any(full_name in source_namespaces for full_name in candidate.get("full_names", []))
    ]
    if len(same_namespace) == 1:
        return same_namespace[0]
    if len(same_namespace) > 1:
        return None

    imported_namespaces = {f"{namespace}.{type_name}" for namespace in source_entry.get("usings", []) if namespace}
    imported_matches = [
        candidate for candidate in candidates if any(full_name in imported_namespaces for full_name in candidate.get("full_names", []))
    ]
    if len(imported_matches) == 1:
        return imported_matches[0]
    if len(imported_matches) > 1:
        return None

    if len(candidates) == 1:
        return candidates[0]
    return None


def _enrich_project_assets(files: list[dict], projects: dict[str, dict]):
    for entry in files:
        for project_path in entry_project_memberships(entry):
            if project_path not in projects:
                continue
            project = projects[project_path]
            path_lower = entry["path"].lower()
            if path_lower.endswith(("app.xaml", "app.axaml")):
                project["app_xaml"] = entry["path"]
                if entry.get("code_behind"):
                    project["app_code_behind"] = entry["code_behind"]
            if path_lower.endswith(".appxmanifest"):
                project["package_manifest"] = entry["path"]
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
                        project[key] = entry[key]
            if path_lower.endswith(".manifest"):
                project.setdefault("application_manifest_path", entry["path"])
                for key in ("assembly_identity", "assembly_version", "requested_execution_level", "manifest_kind"):
                    if entry.get(key):
                        project[key] = entry[key]


def _infer_solution_startup(graph: dict, projects: dict[str, dict]):
    membership: dict[str, list[str]] = defaultdict(list)
    for edge in graph.get("edges", []):
        if edge.get("kind") == "solution-project":
            membership[edge["from"]].append(edge["to"])

    node_map = {node["id"]: node for node in graph.get("nodes", [])}
    for solution_id, members in membership.items():
        scored: list[tuple[int, str, str]] = []
        for project_id in members:
            project = projects.get(project_id, {})
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
        if not solution_node:
            continue
        if len(best) == 1:
            solution_node["startup_project"] = best[0][1]
            solution_node["startup_inference"] = best[0][2]
            if best[0][1] in node_map:
                node_map[best[0][1]]["startup"] = True
        else:
            solution_node["startup_candidates"] = [item[1] for item in best]


def _build_project_graph(files: list[dict], projects: dict[str, dict], known_paths: set[str], name_index: dict[str, list[str]]) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()

    for entry in files:
        path = entry["path"]
        suffix = PurePosixPath(path).suffix.lower()
        if suffix in {".sln", ".slnx"}:
            nodes.append(
                {
                    "id": path,
                    "kind": "solution",
                    "name": PurePosixPath(path).stem,
                    "path": path,
                    "project_count": len(entry.get("solution_projects", [])),
                }
            )
            for project in entry.get("solution_projects", []):
                target = _resolve_analysis_reference(path, project["path"], known_paths, name_index)
                if target:
                    _add_dependency_edge(edges, seen_edges, path, target, "solution-project")
        elif path in projects:
            node = {
                "id": path,
                "kind": "project",
                "name": projects[path]["name"],
                "path": path,
                "project_kind": projects[path]["kind"],
            }
            if projects[path].get("desktop_targets"):
                node["desktop_targets"] = projects[path]["desktop_targets"]
            if projects[path].get("target_frameworks"):
                node["target_frameworks"] = projects[path]["target_frameworks"]
            if projects[path].get("output_type"):
                node["output_type"] = projects[path]["output_type"]
            if projects[path].get("project_role") != "application":
                node["project_role"] = projects[path]["project_role"]
            if projects[path].get("packaging_model"):
                node["packaging_model"] = projects[path]["packaging_model"]
            for key in ("app_xaml", "package_manifest", "package_identity", "package_entry_point"):
                if projects[path].get(key):
                    node[key] = projects[path][key]
            nodes.append(node)

    for entry in files:
        source = entry["path"]
        if source not in projects:
            continue
        for ref in entry.get("project_references", []):
            target = _resolve_analysis_reference(source, ref, known_paths, name_index)
            if target:
                _add_dependency_edge(edges, seen_edges, source, target, "project-reference")

    graph = {"nodes": nodes, "edges": edges}
    _infer_solution_startup(graph, projects)
    return graph


def _desktop_conflict_zones(files: list[dict]) -> list[dict]:
    zones: list[dict] = []
    by_project: dict[str, list[dict]] = defaultdict(list)
    for entry in files:
        for project_path in entry_project_memberships(entry):
            by_project[project_path].append(entry)

    for entry in files:
        if entry.get("code_behind"):
            _append_conflict_zone(
                zones,
                [entry["path"], entry["code_behind"]],
                _code_behind_conflict_reason(entry["path"]),
            )

    for project_path, project_files in by_project.items():
        xaml_by_name = {
            PurePosixPath(item["path"]).name.lower(): item for item in project_files if item["path"].lower().endswith((".xaml", ".axaml"))
        }
        app_xaml = xaml_by_name.get("app.xaml") or xaml_by_name.get("app.axaml")
        if app_xaml:
            startup_files = [project_path, app_xaml["path"], app_xaml.get("code_behind", "")]
            _append_conflict_zone(zones, startup_files, "desktop app startup surface")

        for shell_name in SHELL_NAMES:
            shell_entry = xaml_by_name.get(shell_name)
            if shell_entry:
                shell_files = [project_path, shell_entry["path"], shell_entry.get("code_behind", "")]
                _append_conflict_zone(zones, shell_files, "desktop shell surface")

        resource_files = [item["path"] for item in project_files if is_xaml_resource_entry(item)]
        if resource_files:
            _append_conflict_zone(zones, [project_path, *resource_files], "shared desktop resource dictionary")
        package_manifests = [item["path"] for item in project_files if item["path"].lower().endswith(".appxmanifest")]
        if package_manifests:
            _append_conflict_zone(zones, [project_path, package_manifests[0]], "desktop packaging surface")
        process_manifests = [item["path"] for item in project_files if item["path"].lower().endswith(".manifest")]
        if process_manifests:
            _append_conflict_zone(zones, [project_path, process_manifests[0]], "desktop process manifest")

    return zones


def _cpp_conflict_zones(files: list[dict]) -> list[dict]:
    zones: list[dict] = []
    headers_by_basename: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entry in files:
        path = entry.get("path", "")
        pure = PurePosixPath(path)
        if pure.suffix.lower() not in _CPP_HEADER_SUFFIXES:
            continue
        headers_by_basename[(pure.parent.as_posix(), pure.stem)].append(path)

    for entry in files:
        path = entry.get("path", "")
        pure = PurePosixPath(path)
        if pure.suffix.lower() not in _CPP_SOURCE_SUFFIXES:
            continue
        matches = headers_by_basename.get((pure.parent.as_posix(), pure.stem), [])
        if len(matches) == 1:
            _append_conflict_zone(zones, [path, matches[0]], "cpp header-source pair")

    return zones


def _code_behind_edge_kind(source_path: str) -> str:
    return "razor-code-behind" if source_path.lower().endswith(".razor") else "xaml-code-behind"


def _code_behind_conflict_reason(source_path: str) -> str:
    return "razor-code-behind pair" if source_path.lower().endswith(".razor") else "xaml-code-behind pair"


def _database_conflict_zones(files: list[dict]) -> list[dict]:
    zones: list[dict] = []
    schema_by_project: dict[str, list[str]] = defaultdict(list)
    migrations_by_project: dict[str, list[str]] = defaultdict(list)

    for entry in files:
        if not is_database_file(entry):
            continue
        for project_path in entry_project_memberships(entry):
            schema_by_project[project_path].append(entry["path"])
            if looks_like_database_migration(entry["path"]):
                migrations_by_project[project_path].append(entry["path"])

    for project_path, schema_files in schema_by_project.items():
        _append_conflict_zone(zones, [project_path, *schema_files], "database schema surface")
    for project_path, migration_files in migrations_by_project.items():
        _append_conflict_zone(zones, [project_path, *migration_files], "database migration surface")
    return zones


def run_basic_analysis(request: AnalysisRequest) -> dict:
    """Build a structured project map using stdlib-only heuristics."""
    root = request.root
    cfg = request.cfg
    module_map = _get_module_map(root, cfg)
    first_party = _get_first_party(root, cfg)

    files: list[dict] = []
    for path in _iter_analysis_files(root, cfg):
        rel = str(path.relative_to(root)).replace("\\", "/")
        entry: dict = {
            "path": rel,
            "lines": _count_lines(path),
            "category": _classify_file(rel, module_map),
        }

        suffix = path.suffix.lower()
        name = path.name

        if suffix == ".py":
            entry["imports"] = _extract_python_imports(path, first_party)
            if entry["lines"] > 50:
                defs = _extract_python_definitions(path)
                if defs["classes"]:
                    entry["classes"] = defs["classes"]
                if defs["functions"]:
                    entry["top_functions"] = defs["functions"][:20]
        elif suffix == ".cs":
            entry.update({key: value for key, value in _extract_csharp_metadata(path).items() if value})
        elif suffix == ".razor":
            entry.update({key: value for key, value in _extract_razor_metadata(path).items() if value})
            candidate_rel = PurePosixPath(f"{rel}.cs").as_posix()
            if (root / candidate_rel).exists():
                entry["code_behind"] = candidate_rel
        elif suffix in {".cpp", ".cxx", ".cc", ".c", ".hpp", ".hxx", ".hh", ".h"}:
            entry.update({key: value for key, value in _extract_cpp_metadata(path).items() if value})
        elif suffix in {".xaml", ".axaml"}:
            entry.update(_extract_xaml_metadata(path))
            candidate_rel = PurePosixPath(f"{rel}.cs").as_posix()
            if (root / candidate_rel).exists():
                entry["code_behind"] = candidate_rel
        elif suffix in {".csproj", ".props", ".targets", ".vcxproj", ".wapproj", ".sqlproj"}:
            entry.update({key: value for key, value in _extract_msbuild_project_metadata(path).items() if value})
        elif suffix in {".sln", ".slnx"}:
            entry.update({key: value for key, value in _extract_solution_metadata(path).items() if value})
        elif suffix in {".appxmanifest", ".manifest"}:
            entry.update({key: value for key, value in _extract_manifest_metadata(path).items() if value})
        elif suffix == ".cmake" or name == "CMakeLists.txt":
            entry.update({key: value for key, value in _extract_cmake_metadata(path).items() if value})

        files.append(entry)

    projects = _build_project_index(files)
    _assign_files_to_projects(files, projects)
    _enrich_project_assets(files, projects)

    known_paths = {entry["path"] for entry in files}
    name_index: dict[str, list[str]] = defaultdict(list)
    for entry in files:
        name_index[PurePosixPath(entry["path"]).name].append(entry["path"])
    project_name_index: dict[tuple[str, str], list[str]] = defaultdict(list)
    category_name_index: dict[tuple[str, str], list[str]] = defaultdict(list)
    for entry in files:
        file_name = PurePosixPath(entry["path"]).name
        for project_path in entry_project_memberships(entry):
            project_name_index[(project_path, file_name)].append(entry["path"])
        category_name_index[(entry.get("category", "other"), file_name)].append(entry["path"])

    csharp_type_index = _build_csharp_type_index(files)

    dependency_edges: list[dict] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for entry in files:
        for imp in entry.get("imports", []):
            target = _resolve_python_import_reference(imp, known_paths)
            if target:
                _add_dependency_edge(dependency_edges, seen_edges, entry["path"], target, "python-import")
        for include in entry.get("includes", []):
            target = _resolve_cpp_include_reference(
                entry,
                include,
                known_paths,
                project_name_index,
                category_name_index,
            )
            if target:
                _add_dependency_edge(dependency_edges, seen_edges, entry["path"], target, "cpp-include")
        for ref in entry.get("project_references", []):
            target = _resolve_analysis_reference(entry["path"], ref, known_paths, name_index)
            if target:
                _add_dependency_edge(dependency_edges, seen_edges, entry["path"], target, "project-reference")
        if entry.get("code_behind"):
            _add_dependency_edge(
                dependency_edges,
                seen_edges,
                entry["path"],
                entry["code_behind"],
                _code_behind_edge_kind(entry["path"]),
            )
        if entry["path"].lower().endswith(".cs"):
            resolved_type_names: set[str] = set()
            for candidate in _extract_csharp_reference_candidates(root / entry["path"]):
                resolved = _resolve_csharp_type_reference(entry, candidate, csharp_type_index)
                if resolved:
                    resolved_type_names.add(candidate)
                    _add_dependency_edge(dependency_edges, seen_edges, entry["path"], resolved["path"], "csharp-type-reference")
            if resolved_type_names:
                entry["type_references"] = sorted(resolved_type_names)
        if entry.get("manifest_kind") == "appx" and entry.get("package_entry_point"):
            entry_point_type = str(entry["package_entry_point"]).split("!", 1)[0].split(".")[-1]
            resolved = _resolve_csharp_type_reference(
                {"path": entry["path"], "project": entry.get("project", "")},
                entry_point_type,
                csharp_type_index,
            )
            if resolved:
                _add_dependency_edge(dependency_edges, seen_edges, entry["path"], resolved["path"], "manifest-entry-point")

    project_graph = _build_project_graph(files, projects, known_paths, name_index)

    modules: dict[str, dict] = {}
    for entry in files:
        category = entry["category"]
        if category not in modules:
            modules[category] = {"file_count": 0, "total_lines": 0, "files": []}
        modules[category]["file_count"] += 1
        modules[category]["total_lines"] += entry["lines"]
        modules[category]["files"].append(entry["path"])

    conflict_zones = _get_conflict_zones(cfg)
    for zone in _desktop_conflict_zones(files):
        _append_conflict_zone(conflict_zones, zone["files"], zone["reason"])
    for zone in _cpp_conflict_zones(files):
        _append_conflict_zone(conflict_zones, zone["files"], zone["reason"])
    for zone in _database_conflict_zones(files):
        _append_conflict_zone(conflict_zones, zone["files"], zone["reason"])

    import_index: dict[str, set[str]] = {}
    for entry in files:
        for imp in entry.get("imports", []):
            target = _resolve_python_import_reference(imp, known_paths)
            if target:
                import_index.setdefault(entry["path"], set()).add(target)
    for source, targets in import_index.items():
        for target in targets:
            if target in import_index and source in import_index.get(target, set()):
                _append_conflict_zone(conflict_zones, [source, target], "mutual imports")

    return {
        "provider": BASIC_PROVIDER_META,
        "inventory": {
            "files": files,
            "modules": modules,
            "detected_stacks": _summarize_detected_stacks(files),
            "totals": {
                "files": len(files),
                "lines": sum(entry["lines"] for entry in files),
            },
        },
        "graphs": {
            "dependency_edges": dependency_edges,
            "project_graph": project_graph,
        },
        "signals": {
            "conflict_zones": conflict_zones,
        },
    }


def _resolve_cpp_include_reference(
    source_entry: dict,
    include: str,
    known_paths: set[str],
    project_name_index: dict[tuple[str, str], list[str]],
    category_name_index: dict[tuple[str, str], list[str]],
) -> str:
    normalized_target = include.replace("\\", "/").strip()
    if not normalized_target:
        return ""

    candidate = posixpath.normpath((PurePosixPath(source_entry["path"]).parent / normalized_target).as_posix())
    if candidate in known_paths:
        return candidate

    target_name = PurePosixPath(normalized_target).name
    source_path = source_entry["path"]
    source_project = source_entry.get("project", "")
    if source_project:
        project_matches = [path for path in project_name_index.get((source_project, target_name), []) if path != source_path]
        if len(project_matches) == 1:
            return project_matches[0]
        if len(project_matches) > 1:
            return ""

    source_category = source_entry.get("category", "other")
    category_matches = [path for path in category_name_index.get((source_category, target_name), []) if path != source_path]
    if len(category_matches) == 1:
        return category_matches[0]
    return ""
