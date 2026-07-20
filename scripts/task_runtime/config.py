from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import TypedDict

DEFAULT_CONFIG_RELATIVE_PATH = Path(".codex/skills/project.toml")


class RuntimePaths(TypedDict):
    """Resolved runtime paths derived from the project config."""

    agents_dir: Path
    state_file: Path
    analysis_cache_file: Path
    plans_dir: Path
    tracker_path: str
    tracker_file: Path | None


def parse_toml_simple(path: Path) -> dict:
    """Parse a minimal TOML subset: sections, strings, string arrays, bools."""
    config: dict = {}
    section: str | None = None
    pending_array: list[str] | None = None
    pending_key: str | None = None

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()

        if pending_array is not None:
            pending_array.extend(re.findall(r'"([^"]*)"', stripped))
            if "]" in stripped:
                if section and pending_key:
                    config[section][pending_key] = pending_array
                pending_array = None
                pending_key = None
            continue

        if not stripped or stripped.startswith("#"):
            continue

        section_match = re.match(r"\[([^\]]+)\]", stripped)
        if section_match:
            section = section_match.group(1)
            config.setdefault(section, {})
            continue

        key_match = re.match(r"([\w][\w-]*)\s*=\s*(.*)", stripped)
        if not key_match or not section:
            continue

        key, value = key_match.group(1), key_match.group(2).strip()
        if value.startswith('"') and value.endswith('"'):
            config[section][key] = value[1:-1]
        elif value.startswith("[") and "]" in value:
            config[section][key] = re.findall(r'"([^"]*)"', value)
        elif value.startswith("["):
            pending_array = re.findall(r'"([^"]*)"', value)
            pending_key = key
        elif value.lower() in {"true", "false"}:
            config[section][key] = value.lower() == "true"
        else:
            config[section][key] = value

    if pending_array is not None and section and pending_key:
        config[section][pending_key] = pending_array

    return config


def load_toml_file(path: Path) -> dict:
    try:
        import tomllib

        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (ImportError, ModuleNotFoundError):
        pass

    try:
        import tomli as tomllib  # type: ignore

        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (ImportError, ModuleNotFoundError):
        pass

    return parse_toml_simple(path)


def config_path(root: Path, relative_path: str = ".codex/skills/project.toml") -> Path:
    return root / relative_path


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return load_toml_file(path)


def derive_runtime_paths(root: Path, cfg: dict) -> RuntimePaths:
    paths_cfg = cfg.get("paths", {})
    tracker_path = paths_cfg.get("tracker", "live-tracker.md")
    tracker_file = root / tracker_path if tracker_path else None
    return {
        "agents_dir": root / paths_cfg.get("specs", "agents"),
        "state_file": root / paths_cfg.get("state", "data/tasks.json"),
        "analysis_cache_file": root / paths_cfg.get("analysis_cache", "data/analysis-cache.json"),
        "plans_dir": root / paths_cfg.get("plans", "data/plans"),
        "tracker_path": tracker_path,
        "tracker_file": tracker_file,
    }


def get_module_map(root: Path, cfg: dict) -> dict[str, list[str]]:
    """Return module map from config, or auto-discover from directory structure."""
    modules = cfg.get("modules")
    if modules:
        return modules

    discovered: dict[str, list[str]] = {}

    # Auto-discover: .py files in scripts/ (if it exists) or root/
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        core_files = [f"scripts/{f.name}" for f in sorted(scripts_dir.glob("*.py")) if f.name != "setup.py"]
        if core_files:
            discovered["core"] = core_files
    else:
        core_files = [f.name for f in sorted(root.glob("*.py")) if f.name != "setup.py"]
        if core_files:
            discovered["core"] = core_files

    for directory in sorted(root.iterdir()):
        if (
            directory.is_dir()
            and not directory.name.startswith(".")
            and directory.name
            not in (
                "data",
                "__pycache__",
                "node_modules",
                "dist",
                "bin",
                "obj",
                "scripts",
            )
        ):
            discovered[directory.name] = [f"{directory.name}/"]

    if scripts_dir.is_dir():
        for directory in sorted(scripts_dir.iterdir()):
            if (
                directory.is_dir()
                and not directory.name.startswith(".")
                and directory.name != "__pycache__"
            ):
                discovered[directory.name] = [f"scripts/{directory.name}/"]

    return discovered


def get_first_party(root: Path, cfg: dict) -> set[str]:
    """Return set of first-party module names from config or auto-discovery."""
    modules = get_module_map(root, cfg)
    first_party: set[str] = set()

    for entries in modules.values():
        for raw_path in entries:
            normalized = str(raw_path).replace("\\", "/").strip()
            if not normalized:
                continue
            pure = PurePosixPath(normalized.rstrip("/"))
            if normalized.endswith("/"):
                package_dir = root / pure.as_posix()
                if (package_dir / "__init__.py").is_file():
                    first_party.add(pure.name)
                continue
            if pure.name == "__init__.py" and pure.parent.name:
                first_party.add(pure.parent.name)
                continue
            if pure.suffix == ".py":
                first_party.add(pure.stem)

    return first_party


def get_conflict_zones(cfg: dict) -> list[dict]:
    """Return conflict zones from config."""
    cz_cfg = cfg.get("conflict-zones", {})
    zones_raw = cz_cfg.get("zones", [])
    if not zones_raw:
        return []

    zones = []
    for entry in zones_raw:
        if "|" not in entry:
            continue
        files_part, reason = entry.rsplit("|", 1)
        files = [file_path.strip() for file_path in files_part.split(",") if file_path.strip()]
        zones.append({"files": files, "reason": reason.strip()})
    return zones
