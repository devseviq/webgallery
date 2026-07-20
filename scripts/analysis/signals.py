from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

from .inventory import SHELL_NAMES, entry_project_memberships, is_database_file, is_xaml_resource_entry, looks_like_database_migration


def synthesize_conflict_zones(cfg: dict, files: list[dict]) -> list[dict]:
    zones = configured_conflict_zones(cfg)
    for zone in desktop_conflict_zones(files):
        append_conflict_zone(zones, zone["files"], zone["reason"])
    for zone in cpp_conflict_zones(files):
        append_conflict_zone(zones, zone["files"], zone["reason"])
    for zone in database_conflict_zones(files):
        append_conflict_zone(zones, zone["files"], zone["reason"])
    for zone in mutual_import_conflict_zones(files):
        append_conflict_zone(zones, zone["files"], zone["reason"])
    return zones


def configured_conflict_zones(cfg: dict) -> list[dict]:
    cz_cfg = cfg.get("conflict-zones", {})
    zones_raw = cz_cfg.get("zones", [])
    zones: list[dict] = []
    for entry in zones_raw:
        if "|" not in entry:
            continue
        files_part, reason = entry.rsplit("|", 1)
        files = [file_path.strip() for file_path in files_part.split(",") if file_path.strip()]
        append_conflict_zone(zones, files, reason.strip())
    return zones


def append_conflict_zone(zones: list[dict], files: list[str], reason: str):
    normalized_files = sorted({file_path for file_path in files if file_path})
    if len(normalized_files) < 2:
        return
    zone = {"files": normalized_files, "reason": reason}
    if zone not in zones:
        zones.append(zone)


def desktop_conflict_zones(files: list[dict]) -> list[dict]:
    zones: list[dict] = []
    by_project: dict[str, list[dict]] = defaultdict(list)
    for entry in files:
        for project_path in entry_project_memberships(entry):
            by_project[project_path].append(entry)

    for entry in files:
        if entry.get("code_behind"):
            append_conflict_zone(
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
            append_conflict_zone(
                zones,
                [project_path, app_xaml["path"], app_xaml.get("code_behind", "")],
                "desktop app startup surface",
            )

        for shell_name in SHELL_NAMES:
            shell_entry = xaml_by_name.get(shell_name)
            if shell_entry:
                append_conflict_zone(
                    zones,
                    [project_path, shell_entry["path"], shell_entry.get("code_behind", "")],
                    "desktop shell surface",
                )

        resource_files = [item["path"] for item in project_files if is_xaml_resource_entry(item)]
        if resource_files:
            append_conflict_zone(
                zones,
                [project_path, *resource_files],
                "shared desktop resource dictionary",
            )
        package_manifests = [item["path"] for item in project_files if item["path"].lower().endswith(".appxmanifest")]
        if package_manifests:
            append_conflict_zone(
                zones,
                [project_path, package_manifests[0]],
                "desktop packaging surface",
            )
        process_manifests = [item["path"] for item in project_files if item["path"].lower().endswith(".manifest")]
        if process_manifests:
            append_conflict_zone(
                zones,
                [project_path, process_manifests[0]],
                "desktop process manifest",
            )

    return zones


def cpp_conflict_zones(files: list[dict]) -> list[dict]:
    zones: list[dict] = []
    headers_by_basename: dict[tuple[str, str], list[str]] = defaultdict(list)

    for entry in files:
        path = str(entry.get("path", ""))
        pure = PurePosixPath(path)
        if pure.suffix.lower() not in {".h", ".hh", ".hpp", ".hxx"}:
            continue
        headers_by_basename[(pure.parent.as_posix(), pure.stem)].append(path)

    for entry in files:
        path = str(entry.get("path", ""))
        pure = PurePosixPath(path)
        if pure.suffix.lower() not in {".c", ".cc", ".cpp", ".cxx"}:
            continue
        matches = headers_by_basename.get((pure.parent.as_posix(), pure.stem), [])
        if len(matches) == 1:
            append_conflict_zone(zones, [path, matches[0]], "cpp header-source pair")

    return zones


def database_conflict_zones(files: list[dict]) -> list[dict]:
    zones: list[dict] = []
    schema_by_project: dict[str, list[str]] = defaultdict(list)
    migrations_by_project: dict[str, list[str]] = defaultdict(list)

    for entry in files:
        if not is_database_file(entry):
            continue
        memberships = entry_project_memberships(entry)
        for project_path in memberships:
            schema_by_project[project_path].append(entry["path"])
            if looks_like_database_migration(entry["path"]):
                migrations_by_project[project_path].append(entry["path"])

    for project_path, schema_files in schema_by_project.items():
        append_conflict_zone(zones, [project_path, *schema_files], "database schema surface")
    for project_path, migration_files in migrations_by_project.items():
        append_conflict_zone(zones, [project_path, *migration_files], "database migration surface")
    return zones


def mutual_import_conflict_zones(files: list[dict]) -> list[dict]:
    zones: list[dict] = []
    known_python_paths = {entry["path"] for entry in files if entry.get("path", "").lower().endswith(".py")}
    import_index: dict[str, set[str]] = {}
    for entry in files:
        if not entry.get("path", "").lower().endswith(".py"):
            continue
        for imp in entry.get("imports", []):
            target = _resolve_python_import_path(imp, known_python_paths)
            if target:
                import_index.setdefault(entry["path"], set()).add(target)
    for source, targets in import_index.items():
        for target in targets:
            if target in import_index and source in import_index.get(target, set()):
                append_conflict_zone(zones, [source, target], "mutual imports")
    return zones


def _resolve_python_import_path(import_path: str, known_paths: set[str]) -> str:
    normalized = str(import_path).replace(".", "/").strip("/")
    if not normalized:
        return ""

    for candidate in (f"{normalized}.py", f"{normalized}/__init__.py"):
        if candidate in known_paths:
            return candidate
    return ""


def _code_behind_conflict_reason(source_path: str) -> str:
    return "razor-code-behind pair" if str(source_path).lower().endswith(".razor") else "xaml-code-behind pair"
