from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

ANALYSIS_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class AnalysisRequest:
    """Runtime inputs for a codebase analysis pass."""

    root: Path
    cfg: dict[str, Any]
    generated_at: str
