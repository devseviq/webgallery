"""Validated, path-safe data helpers for the local wallpaper browser."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .content_rating import CONTENT_RATINGS, NSFW_SUBCATEGORIES
from . import index_library as index


RESPONSE_SCHEMA_VERSION = 3
DEFAULT_PAGE_SIZE = 48
MAX_PAGE_SIZE = 100
DEFAULT_SORT = "newest"
MIN_SHUFFLE_SEED = 0
MAX_SHUFFLE_SEED = 2_147_483_647
MAX_AUTOCOMPLETE_PREFIX = 120
MAX_AUTOCOMPLETE_LIMIT = 50
MAX_REVIEWER_LENGTH = 200
MAX_DECISION_NOTE_LENGTH = 2_000

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def _clean_filter(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(str(value))
    except ValueError as exc:
        raise ValueError(f"expected an integer, got {value!r}") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"integer must be between {minimum} and {maximum}")
    return parsed


def _optional_bounded_int(
    value: object, *, minimum: int, maximum: int
) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return _bounded_int(value, default=minimum, minimum=minimum, maximum=maximum)


def _index_snapshot_mtime(db_path: Path) -> float:
    """Return freshness across the main SQLite file and an active WAL."""

    database = Path(db_path)
    snapshot_mtime = database.stat().st_mtime
    try:
        return max(snapshot_mtime, Path(str(database) + "-wal").stat().st_mtime)
    except FileNotFoundError:
        return snapshot_mtime


def _canonical_path_key(value: object) -> str | None:
    """Return an OS-aware comparison key for a configured or reported path."""

    if not isinstance(value, (str, os.PathLike)):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        resolved = Path(value).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return os.path.normcase(os.path.normpath(str(resolved)))


def _canonical_file_exists(row: index.ImageRow, library_root: Path) -> bool:
    """Return whether the indexed row resolves to a current canonical image."""

    path = Path(row.path)
    if not path.is_absolute():
        return False
    try:
        root = Path(library_root).resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, NotADirectoryError, OSError, ValueError):
        return False
    if not resolved.is_file() or resolved.suffix.casefold() not in index.IMAGE_EXTENSIONS:
        return False
    indexed_extension = str(row.ext or "").casefold().lstrip(".")
    return not indexed_extension or indexed_extension == resolved.suffix.casefold().lstrip(".")


def _serialize_tag(tag: index.IndexedTag) -> dict[str, Any]:
    return {
        "name": tag.name,
        "type": tag.type,
        "provenance": tag.provenance,
        "source": tag.source,
    }


def _safe_source_url(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2_048:
        return None
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _serialize_suggestion(suggestion: object) -> dict[str, Any]:
    """Serialize a suggestion without confusing it with provider tags."""

    return {
        "id": int(getattr(suggestion, "id")),
        "label": str(getattr(suggestion, "label")),
        "normalized_label": str(getattr(suggestion, "normalized_label")),
        "confidence": float(getattr(suggestion, "confidence")),
        "generator": str(getattr(suggestion, "generator")),
        "model_version": str(getattr(suggestion, "model_version")),
        "provenance": str(getattr(suggestion, "provenance")),
        "review_status": str(getattr(suggestion, "review_status")),
        "created_at": str(getattr(suggestion, "created_at")),
        "reviewed_at": getattr(suggestion, "reviewed_at"),
        "reviewer": getattr(suggestion, "reviewer"),
        "decision_note": getattr(suggestion, "decision_note"),
    }


def _serialize_row(row: index.ImageRow, library_root: Path) -> dict[str, Any]:
    exists = _canonical_file_exists(row, Path(library_root))
    original_url = f"/original/{row.id}" if exists and row.id > 0 else None
    digest = str(row.sha256 or "")
    thumbnail_url = (
        f"/thumb/{digest.casefold()}.webp"
        if exists and _SHA256_RE.fullmatch(digest)
        else None
    )
    tags = [_serialize_tag(tag) for tag in row.tags]
    provenances = sorted(
        {tag["provenance"] for tag in tags if tag["provenance"]},
        key=str.casefold,
    )
    suggestions = [
        _serialize_suggestion(suggestion) for suggestion in row.tag_suggestions
    ]
    return {
        "id": row.id,
        "url": original_url,
        "original_url": original_url,
        "thumbnail_url": thumbnail_url,
        "exists": exists,
        "filename": row.filename,
        "source": row.source,
        "source_id": row.source_site_id,
        "source_url": _safe_source_url(row.source_url),
        "width": row.width,
        "height": row.height,
        "orientation": row.orientation or "unknown",
        "resolution_bucket": row.resolution_bucket or "_UnknownResolution",
        "extension": row.ext,
        "franchise": row.franchise,
        "purity": row.purity or "unknown",
        "content_rating": row.content_rating,
        "rating_confidence": row.rating_confidence,
        "rating_basis": row.rating_basis,
        "rating_reasons": list(row.rating_reasons),
        "nsfw_subcategory": row.nsfw_subcategory,
        "size_bytes": row.size_bytes,
        "downloaded_at": row.download_recorded_at,
        "sha256": digest.casefold() if _SHA256_RE.fullmatch(digest) else None,
        "tag_count": row.tag_count,
        "enrichment_status": row.enrichment_status,
        "provider_coverage": {
            "status": row.enrichment_status,
            "authoritative_tag_count": row.tag_count,
            "provenances": provenances,
        },
        "tags": tags,
        "tag_suggestions": suggestions,
    }


def query_library_page(
    db_path: Path,
    library_root: Path,
    filters: Mapping[str, object],
) -> dict[str, Any]:
    """Return one validated, paginated, path-safe index page."""

    rating = _clean_filter(filters.get("rating"))
    if rating is not None:
        rating = rating.casefold()
        if rating not in CONTENT_RATINGS:
            raise ValueError("rating must be one of: " + ", ".join(CONTENT_RATINGS))

    nsfw_subcategory = _clean_filter(filters.get("nsfw_subcategory"))
    if nsfw_subcategory is not None:
        nsfw_subcategory = nsfw_subcategory.casefold()
        if nsfw_subcategory not in NSFW_SUBCATEGORIES:
            raise ValueError(
                "nsfw_subcategory must be one of: "
                + ", ".join(NSFW_SUBCATEGORIES)
            )
        if rating != "nsfw":
            raise ValueError("nsfw_subcategory requires rating=nsfw")

    sort = (_clean_filter(filters.get("sort")) or DEFAULT_SORT).casefold()
    if sort not in index.QUERY_SORTS:
        raise ValueError("sort must be one of: " + ", ".join(index.QUERY_SORTS))

    shuffle_seed = _optional_bounded_int(
        filters.get("shuffle_seed"),
        minimum=MIN_SHUFFLE_SEED,
        maximum=MAX_SHUFFLE_SEED,
    )
    if sort == "shuffle" and shuffle_seed is None:
        raise ValueError("shuffle_seed is required when sort is shuffle")

    limit = _bounded_int(
        filters.get("limit"),
        default=DEFAULT_PAGE_SIZE,
        minimum=1,
        maximum=MAX_PAGE_SIZE,
    )
    offset = _bounded_int(
        filters.get("offset"), default=0, minimum=0, maximum=10_000_000
    )

    conn = index.open_index_read_only(Path(db_path))
    try:
        # Pin count, image rows, authoritative tags, and suggestions to one
        # SQLite snapshot even if another WAL connection commits mid-page.
        conn.execute("BEGIN")
        total_indexed = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        rows = index.query(
            conn,
            orientation=_clean_filter(filters.get("orientation")),
            franchise=_clean_filter(filters.get("franchise")),
            bucket=_clean_filter(filters.get("bucket")),
            source=_clean_filter(filters.get("source")),
            tag=_clean_filter(filters.get("tag")),
            content_rating=rating,
            nsfw_subcategory=nsfw_subcategory,
            sort=sort,
            shuffle_seed=shuffle_seed,
            limit=limit + 1,
            offset=offset,
        )
    finally:
        conn.rollback()
        conn.close()

    has_more = len(rows) > limit
    items = [_serialize_row(row, Path(library_root)) for row in rows[:limit]]
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "index": {
            "mtime": datetime.fromtimestamp(
                _index_snapshot_mtime(Path(db_path)), timezone.utc
            ).isoformat(),
            "indexed_images": total_indexed,
        },
        "filters": {
            "rating": rating,
            "nsfw_subcategory": nsfw_subcategory,
            "orientation": _clean_filter(filters.get("orientation")),
            "franchise": _clean_filter(filters.get("franchise")),
            "bucket": _clean_filter(filters.get("bucket")),
            "source": _clean_filter(filters.get("source")),
            "tag": _clean_filter(filters.get("tag")),
            "sort": sort,
            "shuffle_seed": shuffle_seed,
        },
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": len(items),
            "has_more": has_more,
            "missing_paths": sum(not item["exists"] for item in items),
        },
        "items": items,
    }


def _facet_values(
    facets: Mapping[str, Iterable[object]], *names: str
) -> dict[str, int]:
    for name in names:
        values = facets.get(name)
        if values is not None:
            return {
                str(getattr(item, "value") or "unknown"): int(getattr(item, "count"))
                for item in values
            }
    return {}


def library_facets(db_path: Path) -> dict[str, Any]:
    """Return materialized facets plus constant-shape provider coverage."""

    conn = index.open_index_read_only(Path(db_path))
    try:
        materialized = index.read_library_facets(conn)
        coverage = index.provider_coverage(conn)
    finally:
        conn.close()

    ratings = _facet_values(materialized, "rating", "content_rating")
    ratings = {rating: ratings.get(rating, 0) for rating in CONTENT_RATINGS}
    nsfw_subcategories = _facet_values(materialized, "nsfw_subcategory")
    nsfw_subcategories = {
        value: nsfw_subcategories.get(value, 0) for value in NSFW_SUBCATEGORIES
    }
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "indexed_images": int(coverage["total_images"]),
        "ratings": ratings,
        "nsfw_subcategories": nsfw_subcategories,
        "sources": _facet_values(materialized, "source"),
        "orientations": _facet_values(materialized, "orientation"),
        "buckets": _facet_values(materialized, "resolution_bucket", "bucket"),
        "provider_coverage": coverage,
    }


def tag_autocomplete(db_path: Path, prefix: str, limit: int = 20) -> dict[str, Any]:
    """Return counted authoritative-tag matches for WPD's tags endpoint."""

    if not isinstance(prefix, str):
        raise ValueError("prefix must be a string")
    cleaned_prefix = prefix.strip()
    if not 1 <= len(cleaned_prefix) <= MAX_AUTOCOMPLETE_PREFIX:
        raise ValueError(
            f"prefix length must be between 1 and {MAX_AUTOCOMPLETE_PREFIX}"
        )
    bounded_limit = _bounded_int(
        limit, default=20, minimum=1, maximum=MAX_AUTOCOMPLETE_LIMIT
    )
    conn = index.open_index_read_only(Path(db_path))
    try:
        matches = index.counted_tag_autocomplete(
            conn, cleaned_prefix, bounded_limit
        )
    finally:
        conn.close()
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "prefix": cleaned_prefix,
        "limit": bounded_limit,
        "items": [
            {
                "name": item.name,
                "type": item.type,
                "provenance": item.provenance,
                "source": item.source,
                "image_count": item.image_count,
            }
            for item in matches
        ],
    }


def review_tag_suggestion(
    db_path: Path,
    suggestion_id: int,
    *,
    review_status: object,
    reviewer: object,
    decision_note: object = None,
) -> dict[str, Any]:
    """Review one pending suggestion through a validated, non-migrating writer."""

    parsed_id = _bounded_int(
        suggestion_id, default=0, minimum=1, maximum=9_223_372_036_854_775_807
    )
    status = (
        review_status.strip().casefold()
        if isinstance(review_status, str)
        else ""
    )
    if status not in {"accepted", "rejected"}:
        raise ValueError("review_status must be accepted or rejected")
    if not isinstance(reviewer, str):
        raise ValueError("reviewer must be a string")
    reviewer_name = reviewer.strip()
    if not reviewer_name:
        raise ValueError("reviewer is required")
    if len(reviewer_name) > MAX_REVIEWER_LENGTH:
        raise ValueError(f"reviewer must be at most {MAX_REVIEWER_LENGTH} characters")
    if decision_note is not None and not isinstance(decision_note, str):
        raise ValueError("decision_note must be a string or null")
    note = None
    if isinstance(decision_note, str):
        note = decision_note.strip() or None
    if note is not None and len(note) > MAX_DECISION_NOTE_LENGTH:
        raise ValueError(
            f"decision_note must be at most {MAX_DECISION_NOTE_LENGTH} characters"
        )

    conn = index.open_index_write(Path(db_path))
    try:
        with conn:
            suggestion = index.review_tag_suggestion(
                conn,
                parsed_id,
                review_status=status,
                reviewer=reviewer_name,
                decision_note=note,
            )
    finally:
        conn.close()
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "suggestion": _serialize_suggestion(suggestion),
    }


def latest_verification_status(
    reports_root: Path,
    db_path: Path,
    library_root: Path,
) -> dict[str, Any]:
    """Describe whether the configured library matches its latest report."""

    candidate_paths = list(Path(reports_root).glob("maintenance-*/verify.json"))
    if not candidate_paths:
        return {
            "verified": False,
            "reason": "no maintenance verification report found",
            "last_report": None,
        }

    candidates: list[tuple[float, Path]] = []
    for candidate_path in candidate_paths:
        try:
            candidates.append((candidate_path.stat().st_mtime, candidate_path))
        except OSError:
            continue
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)

    configured_database = _canonical_path_key(db_path)
    configured_library = _canonical_path_key(library_root)
    selected: tuple[Path, dict[str, Any], float] | None = None
    for report_mtime, report_path in candidates:
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(report, dict):
            continue
        reported_database = _canonical_path_key(report.get("db_path"))
        reported_library = _canonical_path_key(report.get("library_root"))
        if (
            reported_database is not None
            and configured_database is not None
            and reported_database == configured_database
            and reported_library is not None
            and configured_library is not None
            and reported_library == configured_library
        ):
            selected = (report_path, report, report_mtime)
            break

    if selected is None:
        return {
            "verified": False,
            "reason": (
                "no maintenance verification report matches the configured "
                "database and library root"
            ),
            "last_report": None,
        }

    report_path, report, report_mtime = selected
    snapshot_mtime = _index_snapshot_mtime(Path(db_path))
    report_ok = bool(report.get("ok"))
    unchanged_since_report = snapshot_mtime <= report_mtime
    if not report_ok:
        reason = "last maintenance verification failed"
    elif not unchanged_since_report:
        reason = "index changed after the last successful verification"
    else:
        reason = "last maintenance verification still matches the index file"
    return {
        "verified": report_ok and unchanged_since_report,
        "reason": reason,
        "last_report": report_path.name,
        "last_verified_at": datetime.fromtimestamp(
            report_mtime, timezone.utc
        ).isoformat(),
        "last_status": report.get("status"),
        "last_counts": report.get("counts", {}),
    }
