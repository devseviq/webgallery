"""Read-only data helpers for the local wallpaper library browser."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .content_rating import CONTENT_RATINGS
from . import index_library as index


DEFAULT_PAGE_SIZE = 48
MAX_PAGE_SIZE = 100
DEFAULT_SORT = "newest"


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


def _serialize_row(row: index.ImageRow, library_root: Path) -> dict[str, Any]:
    path = Path(row.path)
    resolved_root = library_root.resolve()
    try:
        relative = path.resolve(strict=False).relative_to(resolved_root)
    except (OSError, ValueError):
        relative = None

    exists = path.is_file()
    url = None
    if relative is not None:
        url = quote("/library/" + relative.as_posix(), safe="/")

    classification = row.content_rating
    return {
        "id": row.id,
        "path": row.path,
        "url": url,
        "exists": exists,
        "filename": row.filename,
        "source": row.source,
        "source_id": row.source_site_id,
        "source_url": row.source_url,
        "width": row.width,
        "height": row.height,
        "orientation": row.orientation or "unknown",
        "resolution_bucket": row.resolution_bucket or "_UnknownResolution",
        "extension": row.ext,
        "franchise": row.franchise,
        "purity": row.purity or "unknown",
        "content_rating": classification.rating,
        "rating_confidence": classification.confidence,
        "rating_basis": classification.basis,
        "rating_reasons": list(classification.reasons),
        "size_bytes": row.size_bytes,
        "downloaded_at": row.download_recorded_at,
        "sha256": row.sha256,
        "tags": [
            {
                "name": tag.name,
                "type": tag.type,
                "provenance": tag.provenance,
                "source": tag.source,
            }
            for tag in row.tags
        ],
    }


def query_library_page(
    db_path: Path,
    library_root: Path,
    filters: Mapping[str, object],
) -> dict[str, Any]:
    """Return one validated, paginated page from the existing index."""

    rating = _clean_filter(filters.get("rating"))
    if rating is not None:
        rating = rating.casefold()
        if rating not in CONTENT_RATINGS:
            raise ValueError("rating must be one of: " + ", ".join(CONTENT_RATINGS))

    sort = (_clean_filter(filters.get("sort")) or DEFAULT_SORT).casefold()
    if sort not in index.QUERY_SORTS:
        raise ValueError("sort must be one of: " + ", ".join(index.QUERY_SORTS))

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
        total_indexed = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        rows = index.query(
            conn,
            orientation=_clean_filter(filters.get("orientation")),
            franchise=_clean_filter(filters.get("franchise")),
            bucket=_clean_filter(filters.get("bucket")),
            source=_clean_filter(filters.get("source")),
            tag=_clean_filter(filters.get("tag")),
            content_rating=rating,
            sort=sort,
            limit=limit + 1,
            offset=offset,
        )
    finally:
        conn.close()

    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_serialize_row(row, Path(library_root)) for row in rows]
    missing_on_page = sum(not item["exists"] for item in items)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "index": {
            "path": str(Path(db_path)),
            "mtime": datetime.fromtimestamp(
                Path(db_path).stat().st_mtime, timezone.utc
            ).isoformat(),
            "indexed_images": total_indexed,
        },
        "filters": {
            "rating": rating,
            "orientation": _clean_filter(filters.get("orientation")),
            "franchise": _clean_filter(filters.get("franchise")),
            "bucket": _clean_filter(filters.get("bucket")),
            "source": _clean_filter(filters.get("source")),
            "tag": _clean_filter(filters.get("tag")),
            "sort": sort,
        },
        "page": {
            "offset": offset,
            "limit": limit,
            "returned": len(items),
            "has_more": has_more,
            "missing_paths": missing_on_page,
        },
        "items": items,
    }


def library_facets(db_path: Path) -> dict[str, Any]:
    """Return global facet counts for the indexed snapshot."""

    conn = index.open_index_read_only(Path(db_path))
    try:
        ratings = {
            row["rating"]: row["count"]
            for row in conn.execute(
                "SELECT rating, COUNT(*) AS count FROM ("
                "SELECT images.id, wallpaper_content_rating("
                "images.purity, GROUP_CONCAT(tags.name, CHAR(31))) AS rating "
                "FROM images "
                "LEFT JOIN image_tags ON image_tags.image_id=images.id "
                "LEFT JOIN tags ON tags.id=image_tags.tag_id "
                "GROUP BY images.id"
                ") GROUP BY rating"
            )
        }

        def grouped(column: str) -> dict[str, int]:
            allowed = {
                "source", "orientation", "resolution_bucket",
            }
            if column not in allowed:
                raise ValueError("unsupported facet column")
            return {
                (row["value"] or "unknown"): row["count"]
                for row in conn.execute(
                    f"SELECT {column} AS value, COUNT(*) AS count "
                    f"FROM images GROUP BY {column} ORDER BY count DESC"
                )
            }

        total = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        return {
            "schema_version": 1,
            "indexed_images": total,
            "ratings": {rating: ratings.get(rating, 0) for rating in CONTENT_RATINGS},
            "sources": grouped("source"),
            "orientations": grouped("orientation"),
            "buckets": grouped("resolution_bucket"),
        }
    finally:
        conn.close()


def latest_verification_status(
    reports_root: Path,
    db_path: Path,
) -> dict[str, Any]:
    """Describe whether the current DB file matches the last verified snapshot."""

    candidates = sorted(
        Path(reports_root).glob("maintenance-*/verify.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {
            "verified": False,
            "reason": "no maintenance verification report found",
            "last_report": None,
        }

    report_path = candidates[0]
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "verified": False,
            "reason": f"latest verification report is unreadable: {exc}",
            "last_report": str(report_path),
        }

    report_mtime = report_path.stat().st_mtime
    db_mtime = Path(db_path).stat().st_mtime
    report_ok = bool(report.get("ok"))
    unchanged_since_report = db_mtime <= report_mtime
    if not report_ok:
        reason = "last maintenance verification failed"
    elif not unchanged_since_report:
        reason = "index changed after the last successful verification"
    else:
        reason = "last maintenance verification still matches the index file"
    return {
        "verified": report_ok and unchanged_since_report,
        "reason": reason,
        "last_report": str(report_path),
        "last_verified_at": datetime.fromtimestamp(
            report_mtime, timezone.utc
        ).isoformat(),
        "last_status": report.get("status"),
        "last_counts": report.get("counts", {}),
    }
