"""Dependency-free helpers for the version 1 wallpaper metadata contract.

Each canonical image may have a sibling ``<image-stem>.wallpaper.json`` file.
The JSON document is the durable source of truth; SQLite is a rebuildable
index.  This module intentionally has no dependency on ``jsonschema`` so the
sort/index tooling remains usable in a stock Python installation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
SOURCES = frozenset({"wallhaven", "zerochan", "anime-pictures", "unknown"})
ORIENTATIONS = frozenset({"landscape", "portrait", "square", "unknown"})
RESOLUTION_BUCKETS = frozenset(
    {"4K", "1440p", "1080p", "720p", "SD", "_UnknownResolution"}
)

_SOURCE_ALIASES = {
    "anime_pictures": "anime-pictures",
    "animepictures": "anime-pictures",
}
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EXTENSION_RE = re.compile(r"^\.[a-z0-9]+$")
_RECORDED_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_CANONICAL_RE = re.compile(
    r"^src=(wallhaven|zerochan|anime-pictures|unknown)"
    r"__id=([A-Za-z0-9][A-Za-z0-9._-]*)"
    r"__size=(\d+)x(\d+)"
    r"__slug=([a-z0-9]+(?:-[a-z0-9]+)*)"
    r"(?:__sha=([0-9a-f]{12}))?"
    r"(\.[a-z0-9]+)$"
)


class MetadataValidationError(ValueError):
    """Raised when a wallpaper sidecar does not satisfy contract v1."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("invalid wallpaper metadata: " + "; ".join(errors))


@dataclass(frozen=True)
class CanonicalFilename:
    """Fields encoded in a canonical, machine-parseable image filename."""

    filename: str
    source: str
    source_id: str
    width: int
    height: int
    slug: str
    collision_hash: str | None
    extension: str


def canonical_source(value: str) -> str:
    """Return the canonical spelling for a source token.

    The underscore alias is retained solely for compatibility with the first
    index schema.  Metadata documents themselves must use canonical values.
    """
    lowered = value.strip().lower()
    return _SOURCE_ALIASES.get(lowered, lowered)


def parse_canonical_filename(filename: str | Path) -> CanonicalFilename | None:
    """Parse ``src=...__id=...__size=...__slug=....ext`` or return ``None``."""
    value = Path(filename).name if isinstance(filename, Path) else filename
    if not isinstance(value, str) or Path(value).name != value:
        return None
    match = _CANONICAL_RE.fullmatch(value)
    if match is None:
        return None
    return CanonicalFilename(
        filename=value,
        source=match.group(1),
        source_id=match.group(2),
        width=int(match.group(3)),
        height=int(match.group(4)),
        slug=match.group(5),
        collision_hash=match.group(6),
        extension=match.group(7),
    )


def sidecar_path_for(image_path: Path) -> Path:
    """Return the v1 sidecar path for an image without touching the disk."""
    return image_path.with_name(f"{image_path.stem}.wallpaper.json")


def validate_metadata(document: Any) -> list[str]:
    """Return all contract violations in ``document`` (an empty list is valid)."""
    errors: list[str] = []
    if not isinstance(document, Mapping):
        return ["$ must be an object"]

    top_fields = {
        "schema_version", "source", "source_id", "source_url",
        "original_filename", "canonical_filename", "slug", "file",
        "classification", "download", "tags", "search_origins",
    }
    _check_exact_fields(document, top_fields, "$", errors)

    schema_version = document.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        errors.append("schema_version must equal 1")

    source = document.get("source")
    if not isinstance(source, str) or source not in SOURCES:
        errors.append("source must be one of anime-pictures, unknown, wallhaven, zerochan")
    _check_nonempty_string(document.get("source_id"), "source_id", errors)
    source_url = document.get("source_url")
    if source_url is not None:
        _check_nonempty_string(source_url, "source_url", errors)
    _check_filename(document.get("original_filename"), "original_filename", errors)
    _check_filename(document.get("canonical_filename"), "canonical_filename", errors)
    _check_slug(document.get("slug"), "slug", errors)

    file_value = document.get("file")
    file_fields = {"sha256", "size_bytes", "extension", "width", "height"}
    _check_object(file_value, file_fields, "file", errors)
    if isinstance(file_value, Mapping):
        sha256 = file_value.get("sha256")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            errors.append("file.sha256 must be 64 hexadecimal characters")
        _check_int(file_value.get("size_bytes"), "file.size_bytes", errors, minimum=0)
        extension = file_value.get("extension")
        if not isinstance(extension, str) or _EXTENSION_RE.fullmatch(extension) is None:
            errors.append("file.extension must be a lowercase extension beginning with '.'")
        _check_int(file_value.get("width"), "file.width", errors, minimum=0)
        _check_int(file_value.get("height"), "file.height", errors, minimum=0)
        # Width was historically required positive; v1 explicitly represents
        # unreadable images as the total pair 0x0.
        width = file_value.get("width")
        height = file_value.get("height")
        if isinstance(width, int) and not isinstance(width, bool):
            if isinstance(height, int) and not isinstance(height, bool):
                if (width == 0) != (height == 0):
                    errors.append("file.width and file.height must both be zero or both be positive")

    classification = document.get("classification")
    _check_object(
        classification, {"resolution_bucket", "orientation"},
        "classification", errors,
    )
    if isinstance(classification, Mapping):
        bucket = classification.get("resolution_bucket")
        orientation = classification.get("orientation")
        if not isinstance(bucket, str) or bucket not in RESOLUTION_BUCKETS:
            errors.append("classification.resolution_bucket is not a supported bucket")
        if not isinstance(orientation, str) or orientation not in ORIENTATIONS:
            errors.append("classification.orientation is not supported")
        if isinstance(file_value, Mapping):
            dims_unknown = file_value.get("width") == 0 and file_value.get("height") == 0
            if dims_unknown and (
                classification.get("resolution_bucket") != "_UnknownResolution"
                or classification.get("orientation") != "unknown"
            ):
                errors.append("0x0 files require _UnknownResolution/unknown classification")
            if not dims_unknown and (
                classification.get("resolution_bucket") == "_UnknownResolution"
                or classification.get("orientation") == "unknown"
            ):
                errors.append("known dimensions require a known bucket and orientation")

    download = document.get("download")
    _check_object(
        download, {"transport", "source_relative_path", "recorded_at"},
        "download", errors,
    )
    if isinstance(download, Mapping):
        _check_nonempty_string(download.get("transport"), "download.transport", errors)
        _check_nonempty_string(
            download.get("source_relative_path"),
            "download.source_relative_path", errors,
        )
        recorded_at = download.get("recorded_at")
        _check_nonempty_string(recorded_at, "download.recorded_at", errors)
        if (
            isinstance(recorded_at, str)
            and recorded_at
            and _RECORDED_AT_RE.fullmatch(recorded_at) is None
        ):
            errors.append("download.recorded_at must be an RFC3339 date-time with timezone")
        elif isinstance(recorded_at, str) and recorded_at:
            try:
                datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
            except ValueError:
                errors.append("download.recorded_at must be an RFC3339 date-time with timezone")

    tags = document.get("tags")
    if not isinstance(tags, list):
        errors.append("tags must be an array")
    else:
        for index, tag in enumerate(tags):
            prefix = f"tags[{index}]"
            _check_object(tag, {"name", "slug", "type", "provenance"}, prefix, errors)
            if not isinstance(tag, Mapping):
                continue
            _check_nonempty_string(tag.get("name"), f"{prefix}.name", errors)
            _check_slug(tag.get("slug"), f"{prefix}.slug", errors)
            _check_nonempty_string(tag.get("type"), f"{prefix}.type", errors)
            _check_nonempty_string(tag.get("provenance"), f"{prefix}.provenance", errors)

    search_origins = document.get("search_origins")
    if not isinstance(search_origins, list):
        errors.append("search_origins must be an array")
    else:
        for index, origin in enumerate(search_origins):
            _check_nonempty_string(origin, f"search_origins[{index}]", errors)

    canonical = document.get("canonical_filename")
    parsed = parse_canonical_filename(canonical) if isinstance(canonical, str) else None
    if isinstance(canonical, str) and parsed is None:
        errors.append("canonical_filename does not use the canonical v1 format")
    if parsed is not None:
        if isinstance(source, str) and source in SOURCES and parsed.source != source:
            errors.append("canonical_filename source does not match source")
        if isinstance(document.get("source_id"), str) and parsed.source_id != document["source_id"]:
            errors.append("canonical_filename id does not match source_id")
        if isinstance(document.get("slug"), str) and parsed.slug != document["slug"]:
            errors.append("canonical_filename slug does not match slug")
        if isinstance(file_value, Mapping):
            if parsed.width != file_value.get("width") or parsed.height != file_value.get("height"):
                errors.append("canonical_filename size does not match file dimensions")
            if parsed.extension != file_value.get("extension"):
                errors.append("canonical_filename extension does not match file.extension")
            sha256 = file_value.get("sha256")
            if (
                parsed.collision_hash is not None
                and isinstance(sha256, str)
                and parsed.collision_hash != sha256.lower()[:12]
            ):
                errors.append("canonical_filename collision suffix does not match file.sha256")

    return errors


def require_valid_metadata(document: Any) -> dict[str, Any]:
    """Validate and return a plain dictionary, raising on contract violations."""
    errors = validate_metadata(document)
    if errors:
        raise MetadataValidationError(errors)
    return dict(document)


def load_metadata(path: Path) -> dict[str, Any]:
    """Load and validate a UTF-8 JSON sidecar."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MetadataValidationError([f"could not load {path}: {exc}"]) from exc
    return require_valid_metadata(document)


def _check_exact_fields(
    value: Mapping[str, Any], expected: set[str], prefix: str, errors: list[str],
) -> None:
    actual = set(value)
    for missing in sorted(expected - actual):
        errors.append(f"{prefix}.{missing} is required")
    for extra in sorted(actual - expected):
        errors.append(f"{prefix}.{extra} is not allowed")


def _check_object(
    value: Any, fields: set[str], prefix: str, errors: list[str],
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{prefix} must be an object")
        return
    _check_exact_fields(value, fields, prefix, errors)


def _check_nonempty_string(value: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{prefix} must be a non-empty string")


def _check_filename(value: Any, prefix: str, errors: list[str]) -> None:
    _check_nonempty_string(value, prefix, errors)
    if isinstance(value, str) and Path(value).name != value:
        errors.append(f"{prefix} must be a filename, not a path")


def _check_slug(value: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
        errors.append(f"{prefix} must be a lowercase hyphen-separated slug")


def _check_int(value: Any, prefix: str, errors: list[str], minimum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        errors.append(f"{prefix} must be an integer >= {minimum}")


__all__ = [
    "CanonicalFilename",
    "MetadataValidationError",
    "ORIENTATIONS",
    "RESOLUTION_BUCKETS",
    "SCHEMA_VERSION",
    "SOURCES",
    "canonical_source",
    "load_metadata",
    "parse_canonical_filename",
    "require_valid_metadata",
    "sidecar_path_for",
    "validate_metadata",
]
