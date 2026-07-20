from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Callable


def normalize_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                items.append(text)
        return items
    text = str(value).strip()
    return [text] if text else []


def extract_markdown_section(text: str, heading: str) -> str:
    pattern = rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def extract_spec_exit_criteria(text: str) -> list[str]:
    section = extract_markdown_section(text, "Exit Criteria")
    if section:
        criteria: list[str] = []
        for line in section.splitlines():
            stripped = line.strip()
            if re.match(r"^[-*]\s+", stripped):
                criteria.append(re.sub(r"^[-*]\s+", "", stripped).strip())
        return [item for item in criteria if item]

    inline_block = re.search(
        r"\*\*Exit criteria:\*\*\s*(.*?)(?=^\s*(?:---|##\s+|###\s+|\*\*[A-Z][^*]+:\*\*)|\Z)",
        text,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if not inline_block:
        return []

    block = inline_block.group(1).strip()
    if not block:
        return []

    criteria = []
    inline_text = block.splitlines()[0].strip() if block.splitlines() else ""
    if inline_text and not re.match(r"^[-*]\s+", inline_text):
        criteria.append(inline_text)
    for line in block.splitlines():
        stripped = line.strip()
        if re.match(r"^[-*]\s+", stripped):
            criteria.append(re.sub(r"^[-*]\s+", "", stripped).strip())
    return [item for item in criteria if item]


def spec_has_placeholders(text: str) -> bool:
    placeholder_patterns = (
        r"\bTODO\b",
        r"\[agent\]",
        r"Details here\.",
        r"list relevant files",
        r"describe what was done",
        r"describe scope",
        r"First task",
        r"Second task",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in placeholder_patterns)


def validate_spec_file(
    path: Path,
    *,
    relative_path: Callable[[Path], str] | None = None,
    agent_id: str | None = None,
    strict: bool = True,
) -> list[str]:
    label = f"Agent {agent_id.upper()}" if agent_id else str(path)
    if not path.exists():
        path_label = relative_path(path) if relative_path else str(path)
        return [f"{label} spec missing: {path_label}"]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{label} spec unreadable: {exc}"]

    errors: list[str] = []
    if not text.strip():
        errors.append(f"{label} spec is empty")
        return errors

    exit_criteria = extract_spec_exit_criteria(text)
    if not exit_criteria:
        errors.append(f"{label} spec has no exit criteria")

    if strict and spec_has_placeholders(text):
        errors.append(f"{label} spec still contains unresolved placeholders")

    return errors


def parse_spec_file(
    path: Path,
    *,
    relative_path: Callable[[Path], str] | None = None,
    error_type: type[Exception] = RuntimeError,
) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise error_type(f"Cannot read spec file {path}: {exc}") from exc

    info: dict = {"spec_file": relative_path(path) if relative_path else str(path)}

    title_match = re.search(r"^#\s+Agent Task\s*[-—]\s*(.+)$", text, re.MULTILINE)
    if title_match:
        info["title"] = title_match.group(1).strip()

    scope_match = re.search(r"\*\*Scope:\*\*\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if scope_match:
        info["scope"] = scope_match.group(1).strip()

    deps_match = re.search(r"\*\*Depends on:\*\*\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if deps_match:
        dep_text = deps_match.group(1).strip()
        if dep_text.lower() not in ("none", "(none)", "—", "-", ""):
            deps = re.findall(r"\bagent\b[\s-]+([a-z]+)\b", dep_text, re.IGNORECASE)
            if not deps:
                for chunk in re.findall(r"\bagents\b([^.;]+)", dep_text, re.IGNORECASE):
                    deps.extend(token for token in re.findall(r"\b[a-z]+\b", chunk.lower()) if token not in {"and", "or", "none"})
            if deps:
                info["deps"] = [dep.lower() for dep in deps]

    files_match = re.search(r"\*\*Output files?:\*\*\s*(.+?)(?:\n|$)", text, re.IGNORECASE)
    if files_match:
        files_text = files_match.group(1).strip()
        if files_text.lower() not in ("none", "(none)", "—", "-", ""):
            info["files"] = [item.strip().strip("`") for item in files_text.split(",")]

    scope_files = re.findall(r"`([^`]+\.\w+)`", info.get("scope", ""))
    if scope_files and "files" not in info:
        info["files"] = scope_files

    exit_criteria = extract_spec_exit_criteria(text)
    if exit_criteria:
        info["exit_criteria"] = exit_criteria

    return info


def parse_tracker(tracker_file: Path | None) -> dict[str, dict]:
    if tracker_file is None or not tracker_file.exists():
        return {}

    try:
        text = tracker_file.read_text(encoding="utf-8")
    except OSError:
        return {}

    entries: dict[str, dict] = {}
    for match in re.finditer(
        r"\|\s*(\S+?)\s*\|\s*(Done|In-progress|Failed)\s*\|"
        r"\s*(\S+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|",
        text,
    ):
        tracker_id = match.group(1)
        raw_status = match.group(2).lower().replace("-", "_")
        entries[tracker_id] = {
            "tracker_id": tracker_id,
            "status": "running" if raw_status == "in_progress" else raw_status,
            "owner": match.group(3),
            "scope_files": match.group(4).strip(),
            "issue": match.group(5).strip(),
            "update": match.group(6).strip(),
        }
    return entries


def build_tracker_prefix_map(state: dict) -> dict[str, str]:
    prefix_map: dict[str, str] = {}
    first_segments: dict[str, list[str]] = defaultdict(list)
    for letter, task in state.get("tasks", {}).items():
        name = task.get("name", "")
        if not name:
            continue
        full_prefix = name.upper().replace("_", "-")
        prefix_map[full_prefix] = letter
        first_segments[name.split("-")[0].upper()].append(letter)

    for prefix, letters in first_segments.items():
        if len(letters) == 1:
            prefix_map[prefix] = letters[0]
    return prefix_map


def configured_runtime_commands(
    command_cfg: dict,
    *,
    profile: str = "default",
    files: list[str] | None = None,
) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = []
    normalized_profile = str(profile or "default").strip().lower()
    if normalized_profile not in {"default", "fast", "full"}:
        normalized_profile = "default"

    compile_cmd = str(command_cfg.get("compile", "") or "").strip()
    if compile_cmd:
        if "{files}" in compile_cmd and not files:
            pass  # skip — no files to check
        else:
            file_arg = " ".join(files or [])
            commands.append(("compile", compile_cmd.replace("{files}", file_arg)))
    build_cmd = str(command_cfg.get("build", "") or "").strip()
    if build_cmd:
        commands.append(("build", build_cmd))

    test_label = "test"
    if normalized_profile == "fast" and str(command_cfg.get("test_fast", "") or "").strip():
        test_label = "test_fast"
    elif normalized_profile == "full" and str(command_cfg.get("test_full", "") or "").strip():
        test_label = "test_full"

    test_cmd = str(command_cfg.get(test_label, "") or "").strip()
    if test_cmd:
        commands.append((test_label, test_cmd))
    return commands


def configured_verification_commands(
    command_cfg: dict,
    files: list[str] | None = None,
    *,
    profile: str = "default",
) -> list[str]:
    commands = [command for _label, command in configured_runtime_commands(command_cfg, profile=profile, files=files)]
    if not commands:
        commands.append("# Add verification commands in .codex/skills/project.toml [commands]")
    return commands


def default_exit_criteria(
    agent: dict,
    *,
    plan: dict | None = None,
    normalize_string_list_fn: Callable[[object], list[str]] | None = None,
) -> list[str]:
    normalize = normalize_string_list_fn or normalize_string_list
    files = normalize(agent.get("files", []))
    scope = str(agent.get("scope", "")).strip() or "The scoped implementation"
    criteria = [
        f"{scope} is implemented in the owned files for Agent {str(agent.get('letter', '?')).upper()}.",
        "All verification commands in this spec complete successfully.",
    ]
    if files:
        criteria.insert(1, f"Owned files updated by this agent: {', '.join(files)}.")
    if plan and plan.get("id"):
        criteria.append(f"The work remains consistent with {plan['id']} plan requirements.")
    return criteria


def build_post_completion_section(
    tracker_path: str,
    tracker_prefix: str,
    scope: str,
    files_str: str,
    owner_label: str,
) -> str:
    if not tracker_path:
        return """\
## Post-completion

No tracker file configured in `.codex/skills/project.toml`.
"""
    return f"""\
## Post-completion

Update `{tracker_path}`:

| ID | Status | Owner | Scope | Issue | Update |
|---|---|---|---|---|---|
| {tracker_prefix}-001 | Done | {owner_label} | {files_str} | {scope} | Completed agent scope and verification. |
"""


def render_spec_template(
    letter: str,
    name: str,
    scope: str,
    *,
    deps: list[str] | None = None,
    files: list[str] | None = None,
    plan: dict | None = None,
    conventions_file: str = "AGENTS.md",
    tracker_path: str = "",
    command_cfg: dict | None = None,
    normalize_string_list_fn: Callable[[object], list[str]] | None = None,
) -> str:
    deps = deps or []
    files = files or []
    command_cfg = command_cfg or {}
    normalize = normalize_string_list_fn or normalize_string_list

    title = name.replace("-", " ").title()
    tracker_prefix = name.upper().replace("_", "-")
    deps_str = ", ".join(f"Agent {dep.upper()}" for dep in deps) or "(none)"
    files_str = ", ".join(f"`{path}`" for path in files) or "No explicit files assigned."
    owner_label = f"agent-{letter.lower()}"
    verification_block = "\n".join(configured_verification_commands(command_cfg, files or None))
    exit_criteria_block = "\n".join(
        f"- {criterion}"
        for criterion in default_exit_criteria(
            {"letter": letter, "name": name, "scope": scope, "files": files},
            plan=plan,
            normalize_string_list_fn=normalize,
        )
    )

    test_cmd = str(command_cfg.get("test", "") or "").strip()
    if test_cmd:
        test_constraint = f"- Existing tests must pass — `{test_cmd}`"
    else:
        test_constraint = "- Existing verification commands must continue to pass"

    post_completion = build_post_completion_section(
        tracker_path,
        tracker_prefix,
        scope,
        files_str,
        owner_label,
    )
    return f"""\
# Agent Task — {title}

**Scope:** {scope}

**Depends on:** {deps_str}

**Output files:** {files_str}

---

## Context — read before doing anything

1. `{conventions_file}`
2. Review the owned files listed above before editing.
3. Respect dependency outputs from: {deps_str}

---

## Task

Implement the scoped change above within the owned files. Keep edits
bounded to this task's declared ownership and preserve interfaces
expected by dependent agents.

## Exit Criteria

{exit_criteria_block}

---

## Constraints

{test_constraint}
- Do not expand scope beyond the files and behaviors listed in this spec.
- Escalate only through the completion summary; do not leave placeholders in the spec.

---

## Verification

```powershell
{verification_block}
```

---

## Do NOT

- Edit files outside the declared ownership without a documented reason.
- Leave the scope partially implemented after running verification.

---

{post_completion}
"""


__all__ = [
    "build_post_completion_section",
    "build_tracker_prefix_map",
    "configured_runtime_commands",
    "configured_verification_commands",
    "default_exit_criteria",
    "extract_markdown_section",
    "extract_spec_exit_criteria",
    "normalize_string_list",
    "parse_spec_file",
    "parse_tracker",
    "render_spec_template",
    "spec_has_placeholders",
    "validate_spec_file",
]
