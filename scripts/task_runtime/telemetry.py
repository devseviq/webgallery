"""Cost telemetry for campaign runtime.

Emits per-step timing and payload metrics into the execution manifest.
Inspired by the pricing/session tracking patterns in Ai_Supervision but
kept lightweight — stdlib-only, no database, no external dependencies.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .state import coerce_int


class StepTimer:
    """Context manager that records elapsed wall-clock milliseconds."""

    __slots__ = ("label", "started", "elapsed_ms")

    def __init__(self, label: str) -> None:
        self.label = label
        self.started: int = 0
        self.elapsed_ms: float = 0.0

    def __enter__(self) -> "StepTimer":
        self.started = time.perf_counter_ns()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.elapsed_ms = round((time.perf_counter_ns() - self.started) / 1_000_000, 1)


def build_telemetry_payload(
    *,
    timers: list[StepTimer] | None = None,
    analysis_json_bytes: int = 0,
    launched_agents: int = 0,
    failed_agents: int = 0,
    model_breakdown: dict[str, int] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a telemetry dict suitable for inclusion in execution manifest JSON.

    Returns a flat dict with keys like ``analyze_ms``, ``preflight_ms``, etc.
    plus aggregate counts.  All values are JSON-serializable primitives.
    """
    payload: dict[str, Any] = {}

    if timers:
        for timer in timers:
            payload[f"{timer.label}_ms"] = timer.elapsed_ms

    if analysis_json_bytes:
        payload["analysis_json_bytes"] = analysis_json_bytes
    if launched_agents:
        payload["launched_agents"] = launched_agents
    if failed_agents:
        payload["failed_agents"] = failed_agents
    if model_breakdown:
        payload["model_breakdown"] = dict(model_breakdown)
    if extra:
        payload.update(extra)

    return payload


def measure_json_bytes(obj: Any) -> int:
    """Return the byte length of *obj* serialized as compact JSON."""
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


# ---------------------------------------------------------------------------
# Model cost estimation (offline, no API calls)
# ---------------------------------------------------------------------------

# Approximate per-million-token pricing (USD) for the generic runtime tiers.
# These are offline estimates only; real billing depends on the provider.
_MODEL_PRICING: dict[str, dict[str, float]] = {
    "mini": {"input": 1.00, "output": 5.00},
    "standard": {"input": 3.00, "output": 15.00},
    "max": {"input": 5.00, "output": 25.00},
}

_TIER_TOKEN_BUDGETS: dict[str, dict[str, int]] = {
    "low": {"input": 10_000, "output": 3_000},
    "medium": {"input": 40_000, "output": 10_000},
    "high": {"input": 80_000, "output": 20_000},
}


def load_pricing_config(cfg: dict) -> dict[str, dict[str, float]]:
    """Load pricing configuration from a config dict, with fallback to _MODEL_PRICING.

    Looks for keys like 'mini_input', 'mini_output', etc. in cfg.get("pricing", {}).
    Converts string values to float (handles TOML parser storing unquoted numbers as strings).
    Returns a dict in the same shape as _MODEL_PRICING.
    """
    pricing_cfg = cfg.get("pricing", {})
    result: dict[str, dict[str, float]] = {}

    for model in ("mini", "standard", "max"):
        result[model] = {}
        for token_type in ("input", "output"):
            key = f"{model}_{token_type}"
            if key in pricing_cfg:
                try:
                    result[model][token_type] = float(pricing_cfg[key])
                except (TypeError, ValueError):
                    result[model][token_type] = _MODEL_PRICING[model][token_type]
            else:
                # Fall back to module default
                result[model][token_type] = _MODEL_PRICING[model][token_type]

    return result


def estimate_agent_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    pricing: dict[str, dict[str, float]] | None = None,
) -> float:
    """Return a rough cost estimate in USD for a single agent invocation.

    This is an *offline* estimate using list pricing.  Actual costs depend on
    caching, batching, and contract pricing.

    If *pricing* is provided, use it instead of the module default _MODEL_PRICING.
    """
    if pricing is None:
        pricing = _MODEL_PRICING
    model_pricing = pricing.get(model.lower(), pricing["standard"])
    return round(
        (input_tokens * model_pricing["input"] + output_tokens * model_pricing["output"]) / 1_000_000,
        4,
    )


def estimate_campaign_savings(
    agents: list[dict],
    *,
    avg_input_tokens: int = 50_000,
    avg_output_tokens: int = 10_000,
    use_tiered: bool = True,
) -> dict[str, Any]:
    """Compare estimated cost of the tiered model selection vs. all-max.

    *agents* should be a list of dicts with at least a ``model`` key.
    Returns a dict with ``tiered_usd``, ``max_usd``, ``savings_usd``, and
    ``savings_pct``.

    Token fallback chain (highest priority first):
    1. Actual tokens from agent result (``input_tokens`` / ``output_tokens``).
    2. Per-tier budgets from ``_TIER_TOKEN_BUDGETS`` based on ``complexity``
       — used when *use_tiered* is ``True`` and actual tokens are absent.
    3. Flat averages (``avg_input_tokens`` / ``avg_output_tokens``) — used
       when *use_tiered* is ``False`` or the complexity key is not found.
    """
    tiered = 0.0
    max_total = 0.0
    for a in agents:
        actual_in = coerce_int(a.get("input_tokens", 0) or 0)
        actual_out = coerce_int(a.get("output_tokens", 0) or 0)
        if actual_in > 0:
            in_tok = actual_in
            out_tok = actual_out if actual_out > 0 else avg_output_tokens
        elif use_tiered:
            complexity = a.get("complexity", "medium")
            budget = _TIER_TOKEN_BUDGETS.get(complexity, _TIER_TOKEN_BUDGETS["medium"])
            in_tok = budget["input"]
            out_tok = budget["output"]
        else:
            in_tok = avg_input_tokens
            out_tok = avg_output_tokens
        tiered += estimate_agent_cost_usd(
            a.get("model", "standard"),
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
        max_total += estimate_agent_cost_usd(
            "max",
            input_tokens=in_tok,
            output_tokens=out_tok,
        )
    savings = max_total - tiered
    return {
        "tiered_usd": round(tiered, 4),
        "max_usd": round(max_total, 4),
        "savings_usd": round(savings, 4),
        "savings_pct": round(savings / max_total * 100, 1) if max_total else 0.0,
    }
