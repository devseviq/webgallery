from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import DEFAULT_CONFIG_RELATIVE_PATH, load_toml_file
from .state import TaskRuntimeError, atomic_write, default_state, relative_path, safe_resolve

DEFAULT_TEMPLATE_RELATIVE_PATH = DEFAULT_CONFIG_RELATIVE_PATH.with_name("project.toml.template")

LANGUAGE_DISPLAY = {
    "python": "Python",
    "node": "Node.js",
    "rust": "Rust",
    "go": "Go",
    "dotnet": ".NET",
    "cpp": "C++",
    "unknown": "Unknown",
}

DEFAULT_CONVENTIONS_STUB = """\
# Project Conventions

Add project-specific conventions here.
"""


@dataclass(slots=True)
class InitResult:
    root: Path
    config_path: Path
    state_path: Path
    agents_dir: Path
    plans_dir: Path
    created: list[str] = field(default_factory=list)
    detected: dict | None = None
    config_written: bool = False
    already_initialized: bool = False
    used_template: bool = False


def detect_project_type(root: Path) -> dict:
    """Detect project language and pre-fill commands based on marker files."""
    name = root.name.replace(" ", "-").lower()
    has_tests_dir = (root / "tests").is_dir()

    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "requirements.txt").exists():
        test_cmd = "python -m pytest tests/ -q" if has_tests_dir else "python -m pytest -q"
        return {
            "name": name,
            "language": "python",
            "test": test_cmd,
            "compile": "python -m py_compile {files}",
            "build": "",
            "has_tests_dir": has_tests_dir,
        }

    if (root / "package.json").exists():
        test_cmd = ""
        build_cmd = ""
        try:
            package = json.loads((root / "package.json").read_text(encoding="utf-8"))
            scripts = package.get("scripts", {})
            if scripts.get("test"):
                test_cmd = "npm test"
            else:
                all_deps = {}
                all_deps.update(package.get("dependencies", {}))
                all_deps.update(package.get("devDependencies", {}))
                if "vitest" in all_deps:
                    test_cmd = "npx vitest"
                elif "jest" in all_deps:
                    test_cmd = "npx jest"
            if scripts.get("build"):
                build_cmd = "npm run build"
        except (json.JSONDecodeError, OSError):
            pass
        return {
            "name": name,
            "language": "node",
            "test": test_cmd,
            "compile": "",
            "build": build_cmd,
            "has_tests_dir": has_tests_dir,
        }

    if (root / "Cargo.toml").exists():
        return {
            "name": name,
            "language": "rust",
            "test": "cargo test",
            "compile": "",
            "build": "cargo build",
            "has_tests_dir": has_tests_dir,
        }

    if (root / "go.mod").exists():
        return {
            "name": name,
            "language": "go",
            "test": "go test ./...",
            "compile": "go vet ./...",
            "build": "go build ./...",
            "has_tests_dir": has_tests_dir,
        }

    has_cmake = any(root.glob("CMakeLists.txt")) or any(root.glob("**/CMakeLists.txt"))
    has_vcxproj = any(root.glob("*.vcxproj")) or any(root.glob("**/*.vcxproj"))
    if has_cmake or (
        has_vcxproj
        and not (
            any(root.glob("*.csproj")) or any(root.glob("**/*.csproj")) or any(root.glob("*.wapproj")) or any(root.glob("**/*.wapproj"))
        )
    ):
        return {
            "name": name,
            "language": "cpp",
            "test": "ctest --test-dir build --output-on-failure" if has_cmake else "",
            "compile": "",
            "build": "cmake --build build" if has_cmake else "",
            "has_tests_dir": has_tests_dir,
        }

    has_csproj = any(root.glob("*.csproj")) or any(root.glob("**/*.csproj"))
    has_wapproj = any(root.glob("*.wapproj")) or any(root.glob("**/*.wapproj"))
    has_sln = any(root.glob("*.sln")) or any(root.glob("**/*.sln"))
    has_slnx = any(root.glob("*.slnx")) or any(root.glob("**/*.slnx"))
    if has_csproj or has_wapproj or has_sln or has_slnx:
        return {
            "name": name,
            "language": "dotnet",
            "test": "dotnet test",
            "compile": "",
            "build": "dotnet build",
            "has_tests_dir": has_tests_dir,
        }

    if has_vcxproj:
        return {
            "name": name,
            "language": "cpp",
            "test": "",
            "compile": "",
            "build": "",
            "has_tests_dir": has_tests_dir,
        }

    return {
        "name": name,
        "language": "unknown",
        "test": "",
        "compile": "",
        "build": "",
        "has_tests_dir": has_tests_dir,
    }


def build_init_config(
    root: Path,
    detected: dict,
    *,
    template_path: Path | None = None,
    conventions_path: str = "AGENTS.md",
) -> tuple[str, bool]:
    """Build project.toml content from the template when present, else fallback."""
    test_val = detected.get("test", "")
    compile_val = detected.get("compile", "")
    build_val = detected.get("build", "")
    default_test_example = "python -m pytest tests/ -q"

    test_line = f'test = "{test_val}"'
    test_fast_line = f'test_fast = "{test_val}"' if test_val else f'# test_fast = "{default_test_example}"'
    test_full_line = f'test_full = "{test_val}"' if test_val else f'# test_full = "{default_test_example}"'
    compile_line = f'compile = "{compile_val}"' if compile_val else '# compile = "python -m py_compile {files}"'
    build_line = f'build = "{build_val}"' if build_val else '# build = ""'

    template = template_path or (root / DEFAULT_TEMPLATE_RELATIVE_PATH)
    if template.exists():
        try:
            rendered = template.read_text(encoding="utf-8")
        except OSError as exc:
            raise TaskRuntimeError(f"Cannot read template {template}: {exc}") from exc
        substitutions = {
            "{{PROJECT_NAME}}": json.dumps(detected.get("name", ""), ensure_ascii=False),
            "{{CONVENTIONS_PATH}}": json.dumps(conventions_path, ensure_ascii=False),
            "{{TEST_LINE}}": test_line,
            "{{TEST_FAST_LINE}}": test_fast_line,
            "{{TEST_FULL_LINE}}": test_full_line,
            "{{COMPILE_LINE}}": compile_line,
            "{{BUILD_LINE}}": build_line,
        }
        for token, value in substitutions.items():
            rendered = rendered.replace(token, value)
        return rendered.rstrip() + "\n", True

    fallback = f"""\
# Campaign Skills — Project Configuration
#
# Configure the skill ecosystem for your project.
# Run `python scripts/task_manager.py analyze` to see what auto-discovery finds.
# Only add [modules] and [conflict-zones] overrides when auto-discovery is wrong.
#
# Required: [project].name, [project].conventions, [commands].test

[project]
name = "{detected.get("name", "")}"
conventions = "{conventions_path}"

[paths]
state = "data/tasks.json"
plans = "data/plans"
specs = "agents/"
tracker = "live-tracker.md"

[commands]
{test_line}
{test_fast_line}
{test_full_line}
{compile_line}
{build_line}

[models]
low = "mini"
medium = "standard"
high = "max"

# [modules]
# core = ["src/main.py"]
# tests = ["tests/"]

# [conflict-zones]
# zones = ["file1.py, file2.py | reason they conflict"]

# [analysis]
# include-globs = ["*.cs", "*.xaml", "*.cpp", "*.h", "CMakeLists.txt"]
# exclude-globs = ["generated/**", "packages/**"]

# Optional: only for `$qa smoke` in repos that expose an HTTP app or API.
# [smoke-test]
# start = "<dev-server command>"
# base-url = "http://127.0.0.1:8000"
# endpoints = ["/health"]

# [ship]
# exclude-extra = []
# warn = []
"""
    return fallback, False


def init_project(
    root: Path,
    *,
    force: bool = False,
    config_path: Path | None = None,
    template_path: Path | None = None,
    conventions_path: str = "AGENTS.md",
    detect_project_type_fn: Callable[[Path], dict] = detect_project_type,
    load_toml_file_fn: Callable[[Path], dict] = load_toml_file,
    atomic_write_fn: Callable[[Path, str], None] = atomic_write,
    safe_resolve_fn: Callable[[str | Path, Path], Path] = safe_resolve,
    default_state_factory: Callable[[], dict] = default_state,
) -> InitResult:
    """Reusable implementation of the task-manager init workflow."""
    init_config_path = config_path or (root / DEFAULT_CONFIG_RELATIVE_PATH)
    created: list[str] = []
    detected: dict | None = None
    config_written = False
    used_template = False

    if not init_config_path.exists() or force:
        if init_config_path.exists() and force:
            try:
                shutil.copy2(str(init_config_path), str(init_config_path) + ".bak")
            except OSError:
                pass
        detected = detect_project_type_fn(root)
        config_text, used_template = build_init_config(
            root,
            detected,
            template_path=template_path,
            conventions_path=conventions_path,
        )
        init_config_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_fn(init_config_path, config_text)
        created.append(relative_path(init_config_path, root))
        config_written = True

    rendered_cfg = load_toml_file_fn(init_config_path) if init_config_path.exists() else {}
    project_cfg = rendered_cfg.get("project", {}) if isinstance(rendered_cfg.get("project"), dict) else {}
    path_cfg = rendered_cfg.get("paths", {})
    agents_dir = safe_resolve_fn(path_cfg.get("specs", "agents"), root)
    plans_dir = safe_resolve_fn(path_cfg.get("plans", "data/plans"), root)
    state_path = safe_resolve_fn(path_cfg.get("state", "data/tasks.json"), root)
    configured_conventions = str(project_cfg.get("conventions", conventions_path) or conventions_path).strip()

    agents_dir.mkdir(parents=True, exist_ok=True)
    plans_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    if not state_path.exists():
        atomic_write_fn(state_path, json.dumps(default_state_factory(), ensure_ascii=False) + "\n")
        created.append(relative_path(state_path, root))

    if configured_conventions:
        conventions_file = safe_resolve_fn(configured_conventions, root)
        if not conventions_file.exists():
            atomic_write_fn(conventions_file, DEFAULT_CONVENTIONS_STUB)
            created.append(relative_path(conventions_file, root))

    return InitResult(
        root=root,
        config_path=init_config_path,
        state_path=state_path,
        agents_dir=agents_dir,
        plans_dir=plans_dir,
        created=created,
        detected=detected,
        config_written=config_written,
        already_initialized=not created,
        used_template=used_template,
    )


def format_init_messages(result: InitResult, *, template_relative_path: Path = DEFAULT_TEMPLATE_RELATIVE_PATH) -> list[str]:
    """Render CLI-style output lines for a completed init run."""
    lines: list[str] = []

    if result.config_path.exists() and not result.config_written:
        lines.append(f"  Config already exists: {result.config_path}")
        lines.append("  Use --force to overwrite.")

    detected = result.detected
    if detected:
        language = detected.get("language", "unknown")
        lines.append(f"  Detected: {LANGUAGE_DISPLAY.get(language, language)} project")
        prefilled: list[str] = []
        if detected.get("name"):
            prefilled.append(f'    [project].name = "{detected["name"]}"')
        if detected.get("test"):
            prefilled.append(f'    [commands].test = "{detected["test"]}"')
        if detected.get("compile"):
            prefilled.append(f'    [commands].compile = "{detected["compile"]}"')
        if detected.get("build"):
            prefilled.append(f'    [commands].build = "{detected["build"]}"')
        if prefilled:
            lines.append("  Pre-filled:")
            lines.extend(prefilled)
        if (result.config_path.parent / template_relative_path.name).exists():
            lines.append(f"  Source: {relative_path(result.config_path.parent / template_relative_path.name, result.root)}")

    if result.created:
        lines.append("  Created:")
        lines.extend(f"    {path}" for path in result.created)
        if detected and detected.get("language") == "unknown":
            lines.append("")
            lines.append("  Next: edit .codex/skills/project.toml and fill in [commands].test")
        elif detected:
            lines.append("")
            lines.append("  Config has been generated from the template and pre-filled with detected settings.")
            lines.append("  Review .codex/skills/project.toml and adjust if needed.")
    else:
        lines.append("  Project already initialized.")

    return lines
