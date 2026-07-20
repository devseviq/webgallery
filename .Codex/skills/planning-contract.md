# Planning Contract — Multi-Agent Campaign Structure

This is a shared reference document, not a skill. It defines the reusable
planning elements that any campaign planner must produce. Skills that produce
plans (`$planner`, `$planner --mode refactor`, etc.) read this contract and add
their own domain-specific context on top.

This contract lives in the canonical Git-tracked skill tree under
`.codex/skills`. Treat changes here as versioned API changes for the planning
surface, not as local-only notes.

**Pipeline position:** Discovery → Planning → Execution

Planners may receive a discovery findings document (`docs/discovery-*.md`) as
upstream input. When a findings document is referenced, planners must read it
and incorporate its constraints, risks, and dependency data into the plan
elements below. Discovery is optional — planners can also work from direct
codebase analysis alone.

---

## Plan Elements — Required

Every plan MUST include ALL 13 standard elements. Domain-specific planners may
add more, but never fewer.

### 1. Campaign Title

A short, descriptive name (e.g., "WebSocket Live Push", "Collector Modularization").

### 2. Goal Statement

One paragraph: **what** the campaign achieves and **why** it matters.
Include the original request and any architectural context that motivates it.

### 3. Exit Criteria

Explicit, testable conditions that define "done" for the entire campaign.
Every criterion must be verifiable by running a command or inspecting an artifact.

```
Exit criteria:
- [ ] All source files compile
- [ ] Tests pass with 0 failures
- [ ] New endpoint returns expected shape (curl / smoke test)
- [ ] No regressions in existing API contracts
- [ ] Conventions / docs updated to reflect changes
```

> **Storage note:** In the human-readable campaign document, exit criteria use
> checkbox syntax (`- [ ]`). In the plan JSON file, `exit_criteria` is stored
> as a plain array of strings (one string per criterion, no checkbox prefix).
> The rendering layer converts between the two formats.

A campaign without exit criteria is not a plan — it is a wish.

### 4. Codebase Impact Assessment

A table of every file/module affected:

| File | Current Lines | Change Type | Risk |
|------|--------------|-------------|------|
| `collector.py` | 2300 | modify | high — central module |
| `tests/test_new.py` | new | create | low |

**Change Types:** `create`, `modify`, `delete`, `rename`

**Risk Levels:**
- `low` — isolated file, no shared surface
- `medium` — touches a shared surface or import boundary
- `high` — central module, conflict zone, or entry point

### 5. Agent Roster

A table defining every agent in the campaign:

| Letter | Name | Scope | Deps | Files Owned | Group | Complexity |
|--------|------|-------|------|-------------|-------|------------|
| `am` | `extract-foo` | Extract Foo class into foo.py | — | `foo.py` | 0 | medium |
| `an` | `wire-foo` | Wire foo.py into app.py | `am` | `app.py` | 1 | low |
| `ao` | `test-foo` | Test Foo extraction | `am,an` | `tests/test_foo.py` | 2 | low |

Required columns:
- **Letter** — next sequential identifier
- **Name** — kebab-case, descriptive
- **Scope** — one sentence starting with a verb (Add, Extract, Refactor, Wire, Test)
- **Deps** — comma-separated agent letters, or `—` for none
- **Files Owned** — files this agent creates or modifies (exclusive ownership)
- **Group** — auto-calculated: `0` if no deps, else `1 + max(group of deps)`
- **Complexity** — `low` (< 50 lines), `medium` (50-200 lines), `high` (200+ lines)

### 6. Dependency Graph

Visual representation of execution order:

```
Group 0 (parallel):  [am: extract-foo]
Group 1 (parallel):  [an: wire-foo]
Group 2:             [ao: test-foo]
```

### 7. File Ownership Map

Every file that will be touched mapped to exactly one agent owner.
**No file may appear under two agents.** If unavoidable, the later agent
must depend on the earlier one.

```
foo.py             → am (extract-foo)
app.py             → an (wire-foo)
tests/test_foo.py  → ao (test-foo)
```

### 8. Conflict Zone Analysis

For each known conflict zone, state whether this campaign touches it and
how it is mitigated:

| Conflict Zone | Affected? | Mitigation |
|--------------|-----------|------------|
| `app.py` ↔ `static/index.html` | Yes | `an` owns app.py, index.html untouched |
| `collector.py` ↔ `collector_services.py` | No | — |

If no conflict zones are affected: state "No conflict zones affected."

### 9. Integration Points

Every cross-agent contract — where one agent's output is another's input:

- `am` creates `foo.py` with class `Foo` → `an` imports and wires it
- `an` adds route `/api/foo` → `ao` tests it

If agents are fully independent: state "No cross-agent contracts."

### 10. Schema Changes

If the campaign adds or modifies database tables:
- New version number
- Migration DDL (reference or inline)
- Which agent owns the migration

If none: state "No schema changes required."

### 11. Risk Assessment

Top risks with mitigations:

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Import cycle from extraction | Medium | Medium | Extract interface, not implementation |
| Test coverage gap | Low | Low | Dedicated test agent in group 2 |

### 12. Verification Strategy

What must pass before the campaign is considered done. This is the
**how** behind the exit criteria — the specific commands and checks:

- [ ] Compile check (from `[commands].compile` in project.toml)
- [ ] Test suite (from `[commands].test` in project.toml)
- [ ] Smoke test if applicable
- [ ] No drift between docs and codebase

### 13. Documentation Updates

Changes needed to project docs (conventions file, README, architecture docs, etc.)
after the campaign completes:

- Add new module to architecture table
- Update API endpoint list
- Add new env variables

If none: state "No documentation updates required."

---

## Plan Artifacts — Durable Record

Every campaign plan MUST exist in two durable artifacts:

- **Machine-readable source of truth:** `data/plans/{plan-id}.json`
- **Human-readable campaign record:** `docs/campaign-{plan-id}-{slug}.md`

Runtime state (`data/tasks.json`) is execution state only. It may cache plan
summaries, but it is not the authoritative home for the full 13-element
standard plan.

If the JSON plan file and markdown document diverge, treat the JSON plan file
as authoritative for structure and identifiers, and refresh the markdown
document to match it.

**JSON path:** `data/plans/{plan-id}.json`
**Markdown path:** `docs/campaign-{plan-id}-{slug}.md`

Derive `{slug}` as a short kebab-case name (2-4 words) from the campaign title.
Example: title "WebSocket Live Push" → `campaign-7-websocket-live-push.md`.

**Written by:** `$planner` and `$manager plan`/`go`
**Referenced by:** `$manager review`

**Consumed by:** `$manager verify` reads exit criteria from
`python scripts/task_manager.py plan criteria --json` (which loads the JSON
plan file). The markdown campaign document is derived drift-check material,
not the authoritative source for exit-criteria verification.

### Plan Document Format

```markdown
# Campaign — {Title}

**Plan ID:** {plan-id}
**Date:** {ISO date}
**Status:** draft | approved | rejected | executed | partial
**Plan file:** data/plans/{plan-id}.json
**Plan doc:** docs/campaign-{plan-id}-{slug}.md

---

## 1. Goal

{Goal statement — element 2}

## 2. Exit Criteria

- [ ] {criterion 1}
- [ ] {criterion 2}

## 3. Impact Assessment

| File | Current Lines | Change Type | Risk |
|------|--------------|-------------|------|
| ... | ... | ... | ... |

## 4. Agent Roster

| Letter | Name | Scope | Deps | Files Owned | Group | Complexity |
|--------|------|-------|------|-------------|-------|------------|
| ... | ... | ... | ... | ... | ... | ... |

## 5. Dependency Graph

{Group 0/1/2 visual}

## 6. File Ownership Map

{file → agent mapping}

## 7. Conflict Zone Analysis

| Conflict Zone | Affected? | Mitigation |
|--------------|-----------|------------|
| ... | ... | ... |

## 8. Integration Points

{Cross-agent contracts, or "No cross-agent contracts."}

## 9. Schema Changes

{Migration details, or "No schema changes required."}

## 10. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| ... | ... | ... | ... |

## 11. Verification Strategy

- [ ] {check 1}
- [ ] {check 2}

## 12. Documentation Updates

{List, or "No documentation updates required."}
```

Standard lifecycle statuses are `draft`, `approved`, `rejected`, and
`executed`. Historical/backfill verification may also surface `partial` for
legacy plans; do not invent additional status names in new plan artifacts.

Refactor mode (`$planner --mode refactor`) appends extra elements (R1, R2, R3)
after element 12.

---

## Agent Namespace Prefixes

To prevent filename collisions when multiple projects share agent letter
sequences (e.g. `agent-c1a`), new agent specs MUST use a project namespace
prefix for their letter identifier. Existing files keep their current names.

| Prefix | Project |
|--------|---------|
| `cv-` | Coversight |
| `py-` | pysight |
| `ui-` | uiFrameWork |
| `dh-` | DataHandling |
| `cs-` | codex-skills |

**Example:** A new Coversight agent would be `agent-cv-d2a-extract-parser.md`,
not `agent-d2a-extract-parser.md`.

The central agent registry at `D:\Development\plans\agent-registry.json` tracks
all agent specs and detects collisions. Regenerate it with:

```powershell
D:\Development\plans\scripts\build-agent-registry.ps1
```

---

## Agent Spec Format

Each agent spec file MUST follow this structure:

```markdown
# Agent Task — {Title}

**Scope:** {One-line description — same as roster}

**Depends on:** {Agent X, Agent Y — or "none"}

**Output files:** {`file1.py`, `file2.py` — comma-separated}

## Exit Criteria

- {Testable condition 1}
- {Testable condition 2}

---

## Context — read before doing anything

1. {Project conventions file}
2. {List every source file the agent needs to read, with what to look for}

---

## Task

### Part 1 — {Subtask title}

{Detailed instructions with:}
- Exact file paths and function names
- Expected behavior / signatures
- Code patterns to follow (reference existing patterns in the codebase)

### Part 2 — {Subtask title}

{...repeat for each discrete subtask}

---

## Constraints

- {What files must NOT be modified}
- {What behaviors must NOT change}
- {Scope boundaries — what is explicitly out of scope}

---

## Verification

{Exact commands to run — compile checks, tests, smoke tests}

---

## Do NOT

- {Anti-pattern 1}
- {Anti-pattern 2}
- {Scope violation}
```

**Spec quality checklist** (every spec must pass all):
- [ ] No TODOs or placeholders remain
- [ ] All referenced files exist in the codebase
- [ ] Function names and code locations are accurate (verified by reading source)
- [ ] Instructions are specific enough to execute without clarifying questions
- [ ] Verification commands are copy-pasteable and functional
- [ ] Constraints clearly define the agent's boundary
- [ ] Exit criteria are testable

---

## Decomposition Heuristics

Rules of thumb for splitting work into agents:

| Signal | Action |
|--------|--------|
| Change touches 1 file | Single agent |
| Change touches 2-3 related files | Single agent if < 200 lines total |
| Change touches files in different modules | Separate agents per module |
| New feature + tests | Feature agent + test agent (test depends on feature) |
| Multiple independent features | Parallel agents (same group) |
| Shared seam between agents | Integration agent in later group |
| Schema migration needed | Dedicated migration agent in group 0 |
| Frontend + backend change | Separate agents; frontend depends on backend |
| Refactor of > 300 lines total | Split by function/class boundary into < 300-line agents |

### Ownership rules

- Each file is owned by **at most one agent** at any time.
- If two agents must touch the same file, one depends on the other.
- High-traffic files (entry points, routers, orchestrators) get a single owner;
  other agents that need changes there depend on that owner.
- Prefer 2-6 agents per campaign. More than 6 increases coordination cost.

### Scope rules

- An agent should need **< 300 lines of changes**. If more, split.
- Every agent must be completable in **one session**.
- Specs must be **self-contained** — no clarifying questions needed.
