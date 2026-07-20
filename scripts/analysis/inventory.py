from __future__ import annotations

from pathlib import PurePosixPath

_RESOURCE_DICTIONARY_NAMES = {
    "generic.xaml",
    "generic.axaml",
    "styles.xaml",
    "styles.axaml",
    "colors.xaml",
    "colors.axaml",
}

SHELL_NAMES = {
    "appshell.xaml",
    "appshell.axaml",
    "mainwindow.xaml",
    "mainwindow.axaml",
    "mainpage.xaml",
    "mainpage.axaml",
    "shellpage.xaml",
    "shellpage.axaml",
    "shellwindow.xaml",
    "shellwindow.axaml",
}


def entry_project_memberships(entry: dict) -> list[str]:
    memberships: list[str] = []
    raw = entry.get("project_memberships", [])
    if isinstance(raw, str):
        raw = [raw]
    elif not isinstance(raw, list):
        raw = []

    for value in [*raw, entry.get("project", "")]:
        normalized = str(value).strip()
        if normalized and normalized not in memberships:
            memberships.append(normalized)
    return memberships


def set_entry_project_memberships(entry: dict, projects: list[str]):
    memberships: list[str] = []
    for value in projects:
        normalized = str(value).strip()
        if normalized and normalized not in memberships:
            memberships.append(normalized)

    if memberships:
        entry["project"] = memberships[0]
        entry["project_memberships"] = memberships
        return

    entry.pop("project", None)
    entry.pop("project_memberships", None)


def is_xaml_resource_entry(entry: dict) -> bool:
    root_element = str(entry.get("root_element", "")).strip().split(":")[-1]
    if root_element in {"ResourceDictionary", "Styles"}:
        return True
    path = str(entry.get("path", "")).strip()
    return PurePosixPath(path).name.lower() in _RESOURCE_DICTIONARY_NAMES


def is_database_project_path(path: str) -> bool:
    return PurePosixPath(str(path)).suffix.lower() == ".sqlproj"


def is_database_file(entry: dict) -> bool:
    return str(entry.get("path", "")).lower().endswith(".sql")


def looks_like_database_migration(path: str) -> bool:
    normalized = str(path).replace("\\", "/").lower()
    parts = [part for part in normalized.split("/") if part]
    if any(part in {"migration", "migrations"} for part in parts[:-1]):
        return True
    name = PurePosixPath(normalized).name
    return name.startswith(("v", "v_", "v-", "migration", "migrate"))
