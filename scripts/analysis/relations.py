from __future__ import annotations

import json


def synthesize_dependency_edges(files: list[dict], existing_edges: list[dict]) -> list[dict]:
    synthesized = list(existing_edges)
    seen = {_edge_key(edge) for edge in synthesized}

    for entry in files:
        source = entry.get("path", "")
        target = entry.get("code_behind", "")
        if source and target:
            edge = {"from": source, "to": target, "kind": _code_behind_edge_kind(source)}
            key = _edge_key(edge)
            if key not in seen:
                seen.add(key)
                synthesized.append(edge)

    return synthesized


def _edge_key(edge: dict) -> str:
    return json.dumps(
        {
            "from": edge.get("from", ""),
            "to": edge.get("to", ""),
            "kind": edge.get("kind", ""),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _code_behind_edge_kind(source_path: str) -> str:
    return "razor-code-behind" if str(source_path).lower().endswith(".razor") else "xaml-code-behind"
