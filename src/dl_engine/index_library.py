#!/usr/bin/env python3
"""
Rebuildable SQLite index of the local wallpaper library.

Why this module exists
----------------------
The canonical ``library/<bucket>/<orientation>/<source>`` tree is paired with
durable ``.wallpaper.json`` sidecars. This module recursively rebuilds a
SQLite search index from that tree, preferring valid sidecars and retaining
legacy filename/header inference for older files.

Boundary
--------
This module never moves, renames, deletes, or sorts wallpaper image files —
that stays in PowerShell (``sort-downloads.ps1`` /
``sort-by-orientation.ps1``), per ``project-conventions.md``. It may write the
SQLite index and the library-level Wallhaven enrichment ledger. A regression
test in ``tests/test_index_library.py`` asserts that no move/sort/delete helpers
exist on this module's surface.

Filename conventions parsed
---------------------------
The library holds three distinguishable filename families:

* **Wallhaven** — ``wallhaven_<id>_<W>x<H>.<ext>``, e.g.
  ``wallhaven_g7xd1d_2000x2999.jpg``. The ID is alphanumeric (or, for older
  files, numeric) and the dimensions are embedded. ~81% of the collection.
* **Anime-Pictures** — ``ANIME-PICTURES.NET_-_<id>-<W>x<H>-<tags>.<ext>`` or
  ``ap-<id>-<W>x<H>-<tags>.<ext>``, e.g.
  ``922947-1800x2546-virtual+youtuber-mr.lime.jpg``. The tag suffix uses ``+``
  for spaces and ``-`` as the segment separator (the same format documented at
  ``anime_pictures.py:153-157``). ~2.3% of the collection, but the only family
  whose franchise/characters/tags are recoverable from the filename alone.
* **Unknown** — bare numeric IDs (``3301538.jpg``) and ``anime-*`` generic
  bulk-renamed files. No recoverable source or tags. ~15% of the collection.

Schema
------
Images and source-qualified tags are stored alongside enrichment progress and
schema-version metadata. See ``_SCHEMA_SQL`` and :func:`_migrate_schema`.

CLI
---
::

    # Offline ingest pass (minutes). No network, no file moves.
    python -m dl_engine.index_library

    # Same, on a non-default library root (for tests / isolated trees).
    python -m dl_engine.index_library --library-root D:\\some\\tree

    # Read-only ad-hoc query.
    python -m dl_engine.index_library --query "orientation=portrait source=wallhaven"

    # Wallhaven API enrichment (long; resumable). See --help.
    python -m dl_engine.index_library --enrich
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import Optional, Sequence

from .content_rating import (
    CONTENT_RATINGS,
    classify_content,
    normalize_label,
    register_sqlite_function as register_content_rating_sqlite_function,
)
from .wallpaper_metadata import (
    MetadataValidationError,
    canonical_source,
    load_metadata,
    parse_canonical_filename,
    sidecar_path_for,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths and tunables
# ---------------------------------------------------------------------------

DB_SCHEMA_VERSION = 3
SQLITE_BUSY_TIMEOUT_MS = 30_000
SQLITE_JOURNAL_MODE = "wal"

VERIFY_REPORT_SCHEMA_VERSION = 1
VERIFY_MAX_ISSUES = 200

_VERIFY_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "images": frozenset(
        {
            "id", "path", "filename", "source", "ext", "width", "height",
            "orientation", "resolution_bucket", "source_site_id", "franchise",
            "purity", "enrichment_status", "indexed_at", "metadata_path",
            "source_url", "original_filename", "canonical_filename", "slug",
            "sha256", "size_bytes", "transport", "source_relative_path",
            "download_recorded_at", "search_origins_json",
            "content_rating", "rating_confidence", "rating_basis",
            "rating_reasons_json", "tag_count",
        }
    ),
    "tags": frozenset(
        {
            "id", "name", "category_id", "category", "source", "slug",
            "tag_type", "provenance",
        }
    ),
    "image_tags": frozenset(
        {"image_id", "tag_id", "provenance"}
    ),
    "enrichment_progress": frozenset(
        {"source", "last_processed_source_site_id", "updated_at"}
    ),
    "schema_metadata": frozenset({"key", "value"}),
    "library_facets": frozenset(
        {"facet", "value", "count", "refreshed_at"}
    ),
    "tag_suggestions": frozenset(
        {
            "id", "image_id", "label", "normalized_label", "confidence",
            "generator", "model_version", "provenance", "review_status",
            "created_at", "reviewed_at", "reviewer", "decision_note",
        }
    ),
}

_VERIFY_INCOMPATIBLE_CODES = frozenset(
    {"missing-table", "missing-column", "schema-version-mismatch"}
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

WALLHAVEN_LEDGER_SCHEMA_VERSION = 1
WALLHAVEN_LEDGER_RECORD_TYPE = "wallhaven-enrichment"
WALLHAVEN_LEDGER_PROVENANCE = "wallhaven-api"
WALLHAVEN_LEDGER_RELATIVE_PATH = (
    Path("_metadata") / "wallhaven-enrichment.v1.jsonl"
)

PROVIDER_LEDGER_SCHEMA_VERSION = 1
PROVIDER_LEDGER_RECORD_TYPE = "provider-enrichment"
PROVIDER_LEDGER_RELATIVE_PATH = (
    Path("_metadata") / "provider-enrichment.v1.jsonl"
)
PROVIDER_LEDGER_SOURCES = frozenset({"zerochan", "anime-pictures"})
SUGGESTION_REVIEW_STATUSES = frozenset({"pending", "accepted", "rejected"})
PROVIDER_LEDGER_STATUSES = frozenset({"pending", "ok", "skipped", "failed"})

# Resolution buckets, mirroring sort-downloads.ps1. The long side determines
# the bucket so a portrait 2160x3840 still counts as 4K.
RESOLUTION_BUCKETS: tuple[tuple[str, int], ...] = (
    ("4K", 3840),
    ("1440p", 2560),
    ("1080p", 1920),
    ("720p", 1280),
    ("SD", 0),
)

# Same image-extension set as sort-downloads.ps1.
IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".jfif", ".png", ".bmp", ".gif", ".tif", ".tiff",
     ".webp", ".avif", ".heic"}
)

# Orientation is derived from WxH. A 3% tolerance keeps near-square images
# (e.g. 4096x4096 panoramas that lost a pixel to JPEG rounding) square rather
# than flipping to landscape.
SQUARE_TOLERANCE = 0.03

# Wallhaven API. The 45 req/min limit is documented and applies with or
# without an API key (the key only unlocks NSFW content + saved filters).
WALLHAVEN_DETAIL_URL = "https://wallhaven.cc/api/v1/w/{id}"
WALLHAVEN_RATE_LIMIT_PER_MIN = 45
WALLHAVEN_SLEEP_SECONDS = 1.4  # ~43/min, under the documented ceiling
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 20
MAX_BACKOFF_SECONDS = 3_600.0

# Wallhaven tag categories that name a franchise/series. The first tag in one
# of these categories becomes the row's ``franchise`` value. Tag categories
# observed on the live API include "Anime & Manga", "Games", and "People".
FRANCHISE_CATEGORIES: frozenset[str] = frozenset({"Anime & Manga", "Games"})

# Image-row enrichment states.
STATUS_PENDING = "pending"   # wallhaven rows awaiting the API pass
STATUS_OK = "ok"             # tags written (anime-pictures at ingest, wallhaven after enrich)
STATUS_SKIPPED = "skipped"   # unknown source, or wallhaven 401 (NSFW without key)
STATUS_FAILED = "failed"     # API error after all retries

# Orientation reorg manifest support. Every source uses the same canonical
# bucket/orientation/source layout; this module never moves files itself.
UNKNOWN_ORIENTATION = "unknown"  # for rows with no readable dimensions


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS images (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    path               TEXT NOT NULL UNIQUE,
    filename           TEXT NOT NULL,
    source             TEXT NOT NULL,          -- canonical metadata source token
    ext                TEXT,
    width              INTEGER,
    height             INTEGER,
    orientation        TEXT,                   -- landscape | portrait | square
    resolution_bucket  TEXT,
    source_site_id     TEXT,
    franchise          TEXT,
    purity             TEXT,
    enrichment_status  TEXT NOT NULL,
    indexed_at         TEXT NOT NULL,
    metadata_path      TEXT,
    source_url         TEXT,
    original_filename  TEXT,
    canonical_filename TEXT,
    slug               TEXT,
    sha256             TEXT,
    size_bytes         INTEGER,
    transport          TEXT,
    source_relative_path TEXT,
    download_recorded_at TEXT,
    search_origins_json TEXT NOT NULL DEFAULT '[]',
    content_rating     TEXT NOT NULL DEFAULT 'unknown'
                       CHECK(content_rating IN ('sfw','suggestive','nsfw','unknown')),
    rating_confidence  REAL NOT NULL DEFAULT 0
                       CHECK(rating_confidence >= 0 AND rating_confidence <= 1),
    rating_basis       TEXT NOT NULL DEFAULT 'no-signal',
    rating_reasons_json TEXT NOT NULL DEFAULT '[]',
    tag_count          INTEGER NOT NULL DEFAULT 0 CHECK(tag_count >= 0)
);

CREATE TABLE IF NOT EXISTS tags (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    category_id  INTEGER,
    category     TEXT,
    source       TEXT,
    slug         TEXT,
    tag_type     TEXT,
    provenance   TEXT
);

CREATE TABLE IF NOT EXISTS image_tags (
    image_id  INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
    provenance TEXT,
    PRIMARY KEY (image_id, tag_id)
);

CREATE TABLE IF NOT EXISTS enrichment_progress (
    source                          TEXT PRIMARY KEY,
    last_processed_source_site_id   TEXT,
    updated_at                      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_metadata (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS library_facets (
    facet        TEXT NOT NULL,
    value        TEXT NOT NULL,
    count        INTEGER NOT NULL CHECK(count >= 0),
    refreshed_at TEXT NOT NULL,
    PRIMARY KEY (facet, value)
);

CREATE TABLE IF NOT EXISTS tag_suggestions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id         INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    label            TEXT NOT NULL CHECK(LENGTH(TRIM(label)) > 0),
    normalized_label TEXT NOT NULL CHECK(LENGTH(TRIM(normalized_label)) > 0),
    confidence       REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    generator        TEXT NOT NULL CHECK(LENGTH(TRIM(generator)) > 0),
    model_version    TEXT NOT NULL CHECK(LENGTH(TRIM(model_version)) > 0),
    provenance       TEXT NOT NULL CHECK(LENGTH(TRIM(provenance)) > 0),
    review_status    TEXT NOT NULL DEFAULT 'pending'
                     CHECK(review_status IN ('pending','accepted','rejected')),
    created_at       TEXT NOT NULL,
    reviewed_at      TEXT,
    reviewer         TEXT,
    decision_note    TEXT,
    UNIQUE (image_id, normalized_label, generator, model_version)
);

CREATE INDEX IF NOT EXISTS idx_images_source      ON images(source);
CREATE INDEX IF NOT EXISTS idx_images_orientation ON images(orientation);
CREATE INDEX IF NOT EXISTS idx_images_bucket      ON images(resolution_bucket);
CREATE INDEX IF NOT EXISTS idx_images_site_id     ON images(source_site_id);
CREATE INDEX IF NOT EXISTS idx_image_tags_tag     ON image_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_tag_suggestions_page
    ON tag_suggestions(image_id, review_status, normalized_label, id);
"""


# ---------------------------------------------------------------------------
# Filename classification + parsing
# ---------------------------------------------------------------------------

# wallhaven_<id>_<W>x<H>.<ext>  — id is alphanumeric (current) or numeric (legacy).
_WALLHAVEN_RE = re.compile(
    r"^wallhaven[_-]([A-Za-z0-9]{4,8})[_-](\d{2,5})x(\d{2,5})$",
    re.IGNORECASE,
)

# Anime-Pictures tag-bearing suffix: <id>-<W>x<H>-<tags>. The site's own
# download_image URL uses this format (see anime_pictures.py:153-157); the
# on-disk filename may carry an ``ANIME-PICTURES.NET_-_`` or ``ap-`` prefix.
_AP_PREFIXED_RE = re.compile(
    r"^(?:ANIME-PICTURES\.NET_-_|ap-)?(\d+)-(\d{2,5})x(\d{2,5})(?:-(.+))?$",
    re.IGNORECASE,
)

# Bare numeric ID, optionally with a sorter-added ``_NNNN`` disambiguator.
_BARE_NUMERIC_RE = re.compile(r"^(\d+)(?:_\d{4})?$")


@dataclass(frozen=True)
class Classification:
    """The result of parsing a single filename.

    Attributes:
        source: A canonical metadata source token.
        source_site_id: The site's own ID for the image, if recoverable.
        width: Pixel width from the filename, if present.
        height: Pixel height from the filename, if present.
        tag_suffix: The raw tag string after ``<id>-<W>x<H>-`` for
            anime-pictures (e.g. ``virtual+youtuber-mr.lime``); ``None``
            otherwise.
    """

    source: str
    source_site_id: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    tag_suffix: Optional[str] = None


def classify_filename(stem: str) -> Classification:
    """Classify a filename stem (no extension) into a :class:`Classification`.

    Args:
        stem: The filename without its extension.

    Returns:
        The parsed classification. Unknown files still return a
        ``Classification`` with ``source='unknown'`` and no further data, so
        callers always get a value to store.
    """
    canonical = parse_canonical_filename(stem)
    if canonical is not None:
        return Classification(
            source=canonical.source,
            source_site_id=canonical.source_id,
            width=canonical.width,
            height=canonical.height,
        )

    # ``classify_filename`` historically accepted a stem.  Also accept a full
    # canonical filename so callers do not have to strip its extension first.
    canonical = parse_canonical_filename(Path(stem).name)
    if canonical is not None:
        return Classification(
            source=canonical.source,
            source_site_id=canonical.source_id,
            width=canonical.width,
            height=canonical.height,
        )

    m = _WALLHAVEN_RE.match(stem)
    if m:
        return Classification(
            source="wallhaven",
            source_site_id=m.group(1).lower(),
            width=int(m.group(2)),
            height=int(m.group(3)),
        )

    m = _AP_PREFIXED_RE.match(stem)
    if m:
        return Classification(
            source="anime-pictures",
            source_site_id=m.group(1),
            width=int(m.group(2)),
            height=int(m.group(3)),
            tag_suffix=m.group(4),
        )

    m = _BARE_NUMERIC_RE.match(stem)
    if m:
        # Bare numeric ID with no WxH and no tags — source unrecoverable from
        # the filename alone. Keep the id in case a later pass can identify it.
        return Classification(source="unknown", source_site_id=m.group(1))

    # ``anime-wallpaper-4K-001`` and other generic/bulk-renamed stems.
    return Classification(source="unknown")


@dataclass(frozen=True)
class ParsedTags:
    """Tags parsed from an anime-pictures tag suffix.

    Attributes:
        franchise: The first ``-``-delimited segment, URL-decoded. For
            ``virtual+youtuber-mr.lime`` this is ``"virtual youtuber"``.
        characters: Character-name segments after the franchise, decoded.
        tags: The remaining attribute segments (``single``, ``long hair`` …),
            decoded. Always excludes the franchise and the literal ``single``
            / ``multiple`` count marker (those are recorded as tags too, but
            the count is not promoted to a column).
    """

    franchise: Optional[str]
    characters: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


def _decode_segment(segment: str) -> str:
    """Decode a tag segment: ``+`` and ``%20`` → space, trimmed.

    Anime-Pictures uses ``+`` for spaces inside a tag and ``-`` to separate
    tags, so ``long+hair`` decodes to ``long hair`` and a literal ``-`` inside
    a name (rare) is not recoverable. ``urllib.parse.unquote_plus`` handles
    both ``+`` and ``%20``.
    """
    return urllib.parse.unquote_plus(segment).strip()


def parse_anime_pictures_tags(tag_suffix: str) -> ParsedTags:
    """Parse an anime-pictures tag suffix into franchise/characters/tags.

    The suffix format is ``{franchise}-{characters}-{count}-{attributes}``
    where each ``-`` separates a segment and ``+`` is a space within a
    segment (see ``anime_pictures.py:588`` for a worked example). The
    franchise is always the first segment; segments after it are recorded as
    tags (character names are not distinguishable from attributes without the
    site's tag-type metadata, so we keep them all as tags and let the caller
    filter).

    Args:
        tag_suffix: The raw suffix after ``<id>-<W>x<H>-``, e.g.
            ``virtual+youtuber-mr.lime`` or
            ``genshin+impact-raiden+shogun-single-long+hair-tall+image``.

    Returns:
        The parsed tags. The franchise is the decoded first segment; the
        remaining decoded segments are returned in ``tags``. ``characters`` is
        left empty (the site's type info isn't in the filename).
    """
    if not tag_suffix:
        return ParsedTags(franchise=None)

    segments = [_decode_segment(s) for s in tag_suffix.split("-")]
    segments = [s for s in segments if s]
    if not segments:
        return ParsedTags(franchise=None)

    franchise = segments[0]
    rest = segments[1:]
    return ParsedTags(franchise=franchise, characters=[], tags=rest)


# ---------------------------------------------------------------------------
# Dimensions / orientation
# ---------------------------------------------------------------------------

def derive_orientation(width: Optional[int], height: Optional[int]) -> Optional[str]:
    """Return ``landscape`` | ``portrait`` | ``square`` | ``None``.

    Args:
        width: Pixel width, or ``None`` if unreadable.
        height: Pixel height, or ``None`` if unreadable.

    Returns:
        The orientation, or ``None`` if either dimension is missing or
        non-positive. Near-square (within :data:`SQUARE_TOLERANCE`) is
        reported as ``square``.
    """
    if not width or not height or width <= 0 or height <= 0:
        return None
    ratio = width / height
    if abs(ratio - 1.0) <= SQUARE_TOLERANCE:
        return "square"
    return "landscape" if width > height else "portrait"


def resolution_bucket_for(width: Optional[int], height: Optional[int]) -> Optional[str]:
    """Return the resolution bucket name matching ``sort-downloads.ps1``.

    Args:
        width: Pixel width, or ``None``.
        height: Pixel height, or ``None``.

    Returns:
        One of ``4K``/``1440p``/``1080p``/``720p``/``SD`` keyed off the long
        side, or ``None`` if dimensions are unavailable.
    """
    if not width or not height:
        return None
    long_side = max(width, height)
    for name, threshold in RESOLUTION_BUCKETS:
        if long_side >= threshold:
            return name
    return "SD"


def read_image_dimensions(path: Path) -> tuple[Optional[int], Optional[int]]:
    """Read pixel (width, height) for ``path`` without third-party deps.

    The library's filenames already carry dimensions for Wallhaven and
    anime-pictures (the majority); this reader is only reached for
    unknown-source files. It reads the format header bytes directly
    (JPEG/PNG/WEBP/GIF/BMP), which is cross-platform and avoids the COM and
    System.Drawing dependencies the PowerShell sorter relies on. On any
    failure it returns ``(None, None)`` so the caller still indexes the file
    with an unknown orientation.

    Args:
        path: Absolute path to an image file.

    Returns:
        ``(width, height)`` as ints, or ``(None, None)`` if the header could
        not be read or the format is unrecognized.
    """
    dims = _read_dimensions_from_header(path)
    if dims is not None:
        return dims
    return None, None


def _read_dimensions_from_header(path: Path) -> Optional[tuple[int, int]]:
    """Read (width, height) from the image header bytes, if recognized.

    Supports the formats actually present in the library (JPEG, PNG, WEBP,
    GIF, BMP, AVIF/HEIF). Returns ``None`` for anything we cannot parse; the
    caller then falls back to the Shell tier or records the image as
    unreadable. Only the first few KiB are read.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(64)
            if len(head) < 12:
                return None
            # PNG: 8-byte sig then IHDR width/height (big-endian uint32s).
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                width = int.from_bytes(head[16:20], "big")
                height = int.from_bytes(head[20:24], "big")
                return (width, height) if width and height else None
            # GIF: 'GIF8', then logical screen w/h (little-endian uint16).
            if head[:6] in (b"GIF87a", b"GIF89a"):
                width = int.from_bytes(head[6:8], "little")
                height = int.from_bytes(head[8:10], "little")
                return (width, height) if width and height else None
            # BMP: 'BM', then header DIB width/height at offsets 18/22.
            if head[:2] == b"BM":
                width = int.from_bytes(head[18:22], "little")
                height = abs(int.from_bytes(head[22:26], "little"))
                return (width, height) if width and height else None
            # WEBP: 'RIFF'....'WEBP'. Sub-chunks carry dims in format-specific
            # spots: VP8X (extended) at RIFF+24, VP8 (lossy) and VP8L (lossless)
            # inside the frame header after the chunk header.
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                chunk = head[12:16]
                if chunk == b"VP8X":
                    # Canvas dims: 24-bit little-endian, +1, at RIFF offsets 24/27.
                    width = int.from_bytes(head[24:27], "little") + 1
                    height = int.from_bytes(head[27:30], "little") + 1
                    return (width, height) if width and height else None
                if chunk in (b"VP8 ", b"VP8L"):
                    # Read enough of the chunk to parse the frame header. The
                    # chunk starts at RIFF offset 20: RIFF(4)+size(4)+WEBP(4)
                    # +fourcc(4)+chunk-size(4), then the chunk payload. Inside
                    # a VP8 (lossy) payload the layout is: 3-byte frame tag,
                    # then the frame header's 16-bit width and 16-bit height
                    # (each 14-bit value + 2-bit scale). The real library
                    # files place the dims at payload offsets 6/8.
                    with path.open("rb") as fh2:
                        fh2.seek(20)
                        frame = fh2.read(10)
                    if chunk == b"VP8 " and len(frame) >= 10:
                        w = int.from_bytes(frame[6:8], "little") & 0x3FFF
                        h = int.from_bytes(frame[8:10], "little") & 0x3FFF
                        return (w, h) if w and h else None
                    if chunk == b"VP8L" and len(frame) >= 5:
                        # Lossless: 1 signature byte, then 14-bit w-1 and
                        # 14-bit h-1 packed little-endian across 4 bytes.
                        b0, b1, b2, b3 = frame[1], frame[2], frame[3], frame[4]
                        w = 1 + (b0 | ((b1 & 0x3F) << 8))
                        h = 1 + ((b1 >> 6) | (b2 << 2) | ((b3 & 0x0F) << 10))
                        return (w, h) if w and h else None
                return None
            # JPEG: scan SOFx markers. Needs more than the first 64 bytes
            # because the dimensions live after the EXIF/quant tables.
            if head[:3] == b"\xff\xd8\xff":
                return _read_jpeg_dimensions(path)
            # AVIF/HEIF: ISO BMFF 'ftyp' box then 'meta' with image dims.
            # Parsing the full box graph is heavy; for the library's handful
            # of AVIFs the Shell tier or System.Drawing is the better path.
            return None
    except (OSError, ValueError):
        return None


def _read_jpeg_dimensions(path: Path) -> Optional[tuple[int, int]]:
    """Scan JPEG markers for an SOFx frame to read (width, height)."""
    try:
        with path.open("rb") as fh:
            fh.read(2)  # SOI
            while True:
                marker = fh.read(2)
                if len(marker) < 2 or marker[0] != 0xFF:
                    return None
                code = marker[1]
                if code in (0xD8, 0xD9):  # SOI/EOI
                    continue
                if code == 0xDA:  # SOS — image data follows, no more headers
                    return None
                length_bytes = fh.read(2)
                if len(length_bytes) < 2:
                    return None
                seg_len = int.from_bytes(length_bytes, "big")
                if seg_len < 2:
                    return None
                seg = fh.read(seg_len - 2)
                # SOFx markers (C0-CF, excluding C4/D8-D9/C0 variants): frame.
                if 0xC0 <= code <= 0xCF and code not in (0xC4, 0xC8, 0xCC):
                    if len(seg) >= 5:
                        height = int.from_bytes(seg[1:3], "big")
                        width = int.from_bytes(seg[3:5], "big")
                        if width and height:
                            return (width, height)
                # Else skip this segment (already consumed above).
    except (OSError, ValueError):
        return None
    return None


# ---------------------------------------------------------------------------
# Database layer
# ---------------------------------------------------------------------------

def _stable_tag_id(source: str, name: str, tag_type: str = "unknown") -> int:
    """Return a deterministic, source-qualified signed SQLite integer id."""
    key = (
        f"{canonical_source(source)}\0{name.strip().casefold()}\0"
        f"{tag_type.strip().casefold()}"
    ).encode("utf-8")
    value = int.from_bytes(hashlib.sha256(key).digest()[:8], "big")
    # Keep generated ids negative and within SQLite's signed 64-bit range.
    return -(value & ((1 << 63) - 1) or 1)


def _tag_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return slug or "tag"


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(
    conn: sqlite3.Connection, table: str, name: str, declaration: str,
) -> None:
    if name not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply additive, idempotent migrations through schema version 3."""
    previous_user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    existing_image_columns = _table_columns(conn, "images")
    existing_tag_columns = _table_columns(conn, "tags")
    needs_tag_identity_migration = (
        previous_user_version < 2
        or not {"source", "tag_type", "provenance"}.issubset(
            existing_tag_columns
        )
    )
    derived_columns = {
        "content_rating", "rating_confidence", "rating_basis",
        "rating_reasons_json", "tag_count",
    }
    needs_derived_backfill = (
        previous_user_version < DB_SCHEMA_VERSION
        or not derived_columns.issubset(existing_image_columns)
    )
    image_columns = {
        "metadata_path": "TEXT",
        "source_url": "TEXT",
        "original_filename": "TEXT",
        "canonical_filename": "TEXT",
        "slug": "TEXT",
        "sha256": "TEXT",
        "size_bytes": "INTEGER",
        "transport": "TEXT",
        "source_relative_path": "TEXT",
        "download_recorded_at": "TEXT",
        "search_origins_json": "TEXT NOT NULL DEFAULT '[]'",
        "content_rating": (
            "TEXT NOT NULL DEFAULT 'unknown' "
            "CHECK(content_rating IN ('sfw','suggestive','nsfw','unknown'))"
        ),
        "rating_confidence": (
            "REAL NOT NULL DEFAULT 0 "
            "CHECK(rating_confidence >= 0 AND rating_confidence <= 1)"
        ),
        "rating_basis": "TEXT NOT NULL DEFAULT 'no-signal'",
        "rating_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
        "tag_count": "INTEGER NOT NULL DEFAULT 0 CHECK(tag_count >= 0)",
    }
    for name, declaration in image_columns.items():
        _ensure_column(conn, "images", name, declaration)

    tag_columns = {
        "source": "TEXT",
        "slug": "TEXT",
        "tag_type": "TEXT",
        "provenance": "TEXT",
    }
    for name, declaration in tag_columns.items():
        _ensure_column(conn, "tags", name, declaration)
    _ensure_column(conn, "image_tags", "provenance", "TEXT")
    conn.execute(
        "UPDATE image_tags SET provenance=(SELECT tags.provenance FROM tags "
        "WHERE tags.id=image_tags.tag_id) WHERE provenance IS NULL"
    )

    # Canonicalize the only source spelling changed by metadata contract v1.
    conn.execute(
        "UPDATE images SET source='anime-pictures' WHERE source='anime_pictures'"
    )
    if needs_tag_identity_migration:
        _migrate_legacy_tag_ids(conn)
    conn.execute("DROP INDEX IF EXISTS idx_tags_source_name")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_source_name_type "
        "ON tags(source, name COLLATE NOCASE, tag_type) "
        "WHERE source IS NOT NULL AND tag_type IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_images_source_identity "
        "ON images(source, source_site_id)"
    )
    index_statements = (
        "CREATE INDEX IF NOT EXISTS idx_images_content_rating "
        "ON images(content_rating, id)",
        "CREATE INDEX IF NOT EXISTS idx_images_tag_count "
        "ON images(tag_count, download_recorded_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_images_download_recorded_at "
        "ON images(download_recorded_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_images_size_bytes "
        "ON images(size_bytes DESC, id)",
        "CREATE INDEX IF NOT EXISTS idx_images_franchise "
        "ON images(franchise COLLATE NOCASE, id)",
        "CREATE INDEX IF NOT EXISTS idx_images_rating_confidence "
        "ON images(rating_confidence, tag_count, id)",
        "CREATE INDEX IF NOT EXISTS idx_image_tags_image "
        "ON image_tags(image_id, tag_id)",
        "CREATE INDEX IF NOT EXISTS idx_tag_suggestions_page "
        "ON tag_suggestions(image_id, review_status, normalized_label, id)",
    )
    for statement in index_statements:
        conn.execute(statement)

    if needs_derived_backfill:
        refresh_derived_metadata(conn)
    conn.execute(
        "INSERT INTO schema_metadata(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(DB_SCHEMA_VERSION),),
    )
    conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")


def _migrate_legacy_tag_ids(conn: sqlite3.Connection) -> None:
    """Replace randomized/name-only legacy tag ids with deterministic ids."""
    rows = conn.execute(
        "SELECT it.image_id, it.tag_id, it.provenance AS association_provenance, "
        "i.source AS image_source, "
        "t.name, t.category_id, t.category, t.slug, t.tag_type, t.provenance "
        "FROM image_tags it "
        "JOIN images i ON i.id=it.image_id "
        "JOIN tags t ON t.id=it.tag_id"
    ).fetchall()
    for row in rows:
        source = canonical_source(row["image_source"])
        tag_type = row["tag_type"] or row["category"] or "unknown"
        tag_id = _stable_tag_id(source, row["name"], tag_type)
        # The legacy index only populated Wallhaven tags through the API.
        # Preserve that fact during migration so a later sidecar ingest keeps
        # those expensive enrichment results instead of treating them as
        # replaceable filename inference.
        default_provenance = (
            "wallhaven-api" if source == "wallhaven" else "legacy-index"
        )
        conn.execute(
            "INSERT INTO tags(id, name, category_id, category, source, slug, "
            "tag_type, provenance) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, source=excluded.source, slug=excluded.slug, "
            "category_id=COALESCE(excluded.category_id, tags.category_id), "
            "category=COALESCE(excluded.category, tags.category), "
            "tag_type=COALESCE(excluded.tag_type, tags.tag_type), "
            "provenance=COALESCE(excluded.provenance, tags.provenance)",
            (
                tag_id, row["name"], row["category_id"], row["category"],
                source, row["slug"] or _tag_slug(row["name"]),
                tag_type,
                row["provenance"] or default_provenance,
            ),
        )
        provenance = (
            row["association_provenance"]
            or row["provenance"]
            or default_provenance
        )
        conn.execute(
            "INSERT INTO image_tags(image_id, tag_id, provenance) VALUES (?,?,?) "
            "ON CONFLICT(image_id, tag_id) DO UPDATE SET "
            "provenance=excluded.provenance",
            (row["image_id"], tag_id, provenance),
        )
        if row["tag_id"] != tag_id:
            conn.execute(
                "DELETE FROM image_tags WHERE image_id=? AND tag_id=?",
                (row["image_id"], row["tag_id"]),
            )
    conn.execute(
        "DELETE FROM tags WHERE NOT EXISTS "
        "(SELECT 1 FROM image_tags WHERE image_tags.tag_id=tags.id)"
    )


def _seeded_rank(image_id: object, seed: object) -> int:
    """Return a deterministic positive SQLite integer for shuffle ordering."""
    payload = f"{int(seed)}\0{int(image_id)}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") & (
        (1 << 63) - 1
    )


def _register_index_sqlite_functions(conn: sqlite3.Connection) -> None:
    register_content_rating_sqlite_function(conn)
    conn.create_function(
        "wallpaper_seed_rank", 2, _seeded_rank, deterministic=True,
    )


def _preflight_writable_schema(conn: sqlite3.Connection) -> None:
    """Reject future or contradictory schemas before making any DB change."""
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    metadata_version: Optional[int] = None
    if "schema_metadata" in tables:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(schema_metadata)")
        }
        if not {"key", "value"}.issubset(columns):
            raise ValueError("schema_metadata table is structurally incompatible")
        row = conn.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()
        if row is not None:
            try:
                metadata_version = int(str(row[0]))
            except ValueError as exc:
                raise ValueError(
                    "schema_metadata.schema_version is not an integer"
                ) from exc
    for label, version in (
        ("PRAGMA user_version", user_version),
        ("schema_metadata.schema_version", metadata_version),
    ):
        if version is not None and version > DB_SCHEMA_VERSION:
            raise ValueError(
                f"{label}={version} is newer than supported schema "
                f"{DB_SCHEMA_VERSION}"
            )
        if version is not None and version < 0:
            raise ValueError(f"{label}={version} is invalid")
    if metadata_version is not None and metadata_version != user_version:
        raise ValueError(
            "schema version markers disagree: "
            f"user_version={user_version}, metadata={metadata_version}"
        )
    if user_version in {2, DB_SCHEMA_VERSION} and metadata_version is None:
        raise ValueError(
            f"schema version {user_version} requires schema_metadata marker"
        )


def connect(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the index DB and ensure the schema exists.

    Args:
        db_path: Path to the SQLite file.

    Returns:
        An open ``sqlite3.Connection`` with foreign keys on and
        ``Row``-shaped cursors.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        _preflight_writable_schema(conn)
        journal_mode = conn.execute(
            f"PRAGMA journal_mode = {SQLITE_JOURNAL_MODE}"
        ).fetchone()[0]
        if str(journal_mode).casefold() != SQLITE_JOURNAL_MODE:
            raise sqlite3.OperationalError(
                f"SQLite refused journal_mode={SQLITE_JOURNAL_MODE}: {journal_mode}"
            )
        conn.execute("PRAGMA synchronous = NORMAL")
        _register_index_sqlite_functions(conn)
        conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_SQL)
        _migrate_schema(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    return conn


@dataclass(frozen=True)
class IndexedTag:
    """A source-qualified tag exposed by :func:`query`."""

    name: str
    slug: str
    type: str
    provenance: str
    source: str


@dataclass(frozen=True)
class TagSuggestion:
    """Review-only visual tag evidence; never an authoritative image tag."""

    id: int
    image_id: int
    label: str
    normalized_label: str
    confidence: float
    generator: str
    model_version: str
    provenance: str
    review_status: str
    created_at: str
    reviewed_at: Optional[str]
    reviewer: Optional[str]
    decision_note: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TagSuggestion":
        return cls(
            id=int(row["id"]), image_id=int(row["image_id"]),
            label=str(row["label"]),
            normalized_label=str(row["normalized_label"]),
            confidence=float(row["confidence"]),
            generator=str(row["generator"]),
            model_version=str(row["model_version"]),
            provenance=str(row["provenance"]),
            review_status=str(row["review_status"]),
            created_at=str(row["created_at"]),
            reviewed_at=row["reviewed_at"], reviewer=row["reviewer"],
            decision_note=row["decision_note"],
        )


@dataclass(frozen=True)
class TagAutocompleteResult:
    name: str
    type: str
    provenance: str
    source: str
    image_count: int


@dataclass(frozen=True)
class FacetCount:
    value: str
    count: int
    refreshed_at: str


@dataclass(frozen=True)
class DerivedRefreshResult:
    images: int
    facets: int


class MaterializedContentRating(str):
    """String-compatible materialized rating with legacy explanation access."""

    rating: str
    confidence: float
    basis: str
    reasons: tuple[str, ...]

    def __new__(
        cls, rating: str, confidence: float, basis: str,
        reasons: Iterable[str] = (),
    ) -> "MaterializedContentRating":
        instance = str.__new__(cls, rating)
        instance.rating = rating
        instance.confidence = float(confidence)
        instance.basis = basis
        instance.reasons = tuple(reasons)
        return instance


@dataclass(frozen=True)
class ImageRow:
    """A flat read model for one indexed image, returned by :func:`query`."""

    id: int
    path: str
    filename: str
    source: str
    ext: Optional[str]
    width: Optional[int]
    height: Optional[int]
    orientation: Optional[str]
    resolution_bucket: Optional[str]
    source_site_id: Optional[str]
    franchise: Optional[str]
    purity: Optional[str]
    enrichment_status: str
    metadata_path: Optional[str]
    source_url: Optional[str]
    original_filename: Optional[str]
    canonical_filename: Optional[str]
    slug: Optional[str]
    sha256: Optional[str]
    size_bytes: Optional[int]
    transport: Optional[str]
    source_relative_path: Optional[str]
    download_recorded_at: Optional[str]
    search_origins: tuple[str, ...]
    content_rating: MaterializedContentRating
    rating_confidence: float
    rating_basis: str
    rating_reasons: tuple[str, ...]
    tag_count: int
    tags: tuple[IndexedTag, ...]
    tag_suggestions: tuple[TagSuggestion, ...]

    @classmethod
    def from_row(
        cls, row: sqlite3.Row, tags: Iterable[IndexedTag] = (),
        suggestions: Iterable[TagSuggestion] = (),
    ) -> "ImageRow":
        try:
            origins_value = json.loads(row["search_origins_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            origins_value = []
        origins = tuple(v for v in origins_value if isinstance(v, str))
        try:
            reasons_value = json.loads(row["rating_reasons_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            reasons_value = []
        reasons = tuple(v for v in reasons_value if isinstance(v, str))
        materialized_rating = MaterializedContentRating(
            str(row["content_rating"]), float(row["rating_confidence"]),
            str(row["rating_basis"]), reasons,
        )
        return cls(
            id=row["id"],
            path=row["path"],
            filename=row["filename"],
            source=row["source"],
            ext=row["ext"],
            width=row["width"],
            height=row["height"],
            orientation=row["orientation"],
            resolution_bucket=row["resolution_bucket"],
            source_site_id=row["source_site_id"],
            franchise=row["franchise"],
            purity=row["purity"],
            enrichment_status=row["enrichment_status"],
            metadata_path=row["metadata_path"],
            source_url=row["source_url"],
            original_filename=row["original_filename"],
            canonical_filename=row["canonical_filename"],
            slug=row["slug"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            transport=row["transport"],
            source_relative_path=row["source_relative_path"],
            download_recorded_at=row["download_recorded_at"],
            search_origins=origins,
            content_rating=materialized_rating,
            rating_confidence=materialized_rating.confidence,
            rating_basis=materialized_rating.basis,
            rating_reasons=materialized_rating.reasons,
            tag_count=int(row["tag_count"]),
            tags=tuple(tags),
            tag_suggestions=tuple(suggestions),
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validated_image_ids(image_ids: Iterable[int]) -> tuple[int, ...]:
    values: set[int] = set()
    for value in image_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("image IDs must be positive integers")
        values.add(value)
    return tuple(sorted(values))


def _id_chunks(image_ids: tuple[int, ...], size: int = 900) -> Iterable[tuple[int, ...]]:
    for start in range(0, len(image_ids), size):
        yield image_ids[start:start + size]


def _refresh_library_facets(conn: sqlite3.Connection, refreshed_at: str) -> int:
    """Replace all small global facet rows inside the caller's transaction."""
    conn.execute("DELETE FROM library_facets")
    direct_facets = (
        ("content_rating", "content_rating"),
        ("source", "source"),
        ("orientation", "COALESCE(NULLIF(TRIM(orientation), ''), 'unknown')"),
        (
            "resolution_bucket",
            "COALESCE(NULLIF(TRIM(resolution_bucket), ''), 'unknown')",
        ),
        ("enrichment_status", "enrichment_status"),
    )
    for facet, expression in direct_facets:
        conn.execute(
            "INSERT INTO library_facets(facet, value, count, refreshed_at) "
            f"SELECT ?, {expression}, COUNT(*), ? FROM images "
            f"GROUP BY {expression}",
            (facet, refreshed_at),
        )
    tag_bucket = (
        "CASE WHEN tag_count=0 THEN '0' WHEN tag_count=1 THEN '1' "
        "WHEN tag_count BETWEEN 2 AND 4 THEN '2-4' "
        "WHEN tag_count BETWEEN 5 AND 9 THEN '5-9' ELSE '10+' END"
    )
    conn.execute(
        "INSERT INTO library_facets(facet, value, count, refreshed_at) "
        f"SELECT 'tag_count_bucket', {tag_bucket}, COUNT(*), ? FROM images "
        f"GROUP BY {tag_bucket}",
        (refreshed_at,),
    )
    conn.execute(
        "INSERT INTO library_facets(facet, value, count, refreshed_at) "
        "SELECT 'provider_coverage', source || '|' || enrichment_status, "
        "COUNT(*), ? FROM images GROUP BY source, enrichment_status",
        (refreshed_at,),
    )
    conn.execute(
        "INSERT INTO library_facets(facet, value, count, refreshed_at) "
        "SELECT 'tag_provenance', "
        "COALESCE(t.source, i.source) || '|' || "
        "COALESCE(it.provenance, t.provenance, 'unknown'), "
        "COUNT(DISTINCT it.image_id), ? "
        "FROM image_tags it JOIN images i ON i.id=it.image_id "
        "JOIN tags t ON t.id=it.tag_id "
        "GROUP BY COALESCE(t.source, i.source), "
        "COALESCE(it.provenance, t.provenance, 'unknown')",
        (refreshed_at,),
    )
    return int(conn.execute("SELECT COUNT(*) FROM library_facets").fetchone()[0])


def refresh_derived_metadata(
    conn: sqlite3.Connection,
    image_ids: Optional[Iterable[int]] = None,
    *,
    refresh_facets: bool = True,
) -> DerivedRefreshResult:
    """Materialize ratings/tag counts from authoritative tags only.

    The function participates in the caller's transaction and never commits.
    ``tag_suggestions`` is intentionally absent from every read in this path.
    """
    bounded_ids = None if image_ids is None else _validated_image_ids(image_ids)
    conn.execute("SAVEPOINT refresh_derived_metadata")
    try:
        if bounded_ids is None:
            image_rows = conn.execute(
                "SELECT id, purity FROM images ORDER BY id"
            ).fetchall()
        elif not bounded_ids:
            image_rows = []
        else:
            image_rows = []
            for chunk in _id_chunks(bounded_ids):
                placeholders = ",".join("?" for _ in chunk)
                image_rows.extend(
                    conn.execute(
                        f"SELECT id, purity FROM images WHERE id IN ({placeholders}) "
                        "ORDER BY id",
                        chunk,
                    ).fetchall()
                )

        selected_ids = tuple(int(row["id"]) for row in image_rows)
        tags_by_image: dict[int, list[dict[str, str]]] = {
            image_id: [] for image_id in selected_ids
        }
        if bounded_ids is None:
            tag_rows = conn.execute(
                "SELECT it.image_id, t.name, t.tag_type, t.category, "
                "COALESCE(it.provenance, t.provenance, 'unknown') AS provenance "
                "FROM image_tags it JOIN tags t ON t.id=it.tag_id "
                "ORDER BY it.image_id, t.name COLLATE NOCASE, t.name, "
                "COALESCE(t.tag_type, t.category, 'unknown') COLLATE NOCASE, t.id"
            ).fetchall()
        else:
            tag_rows = []
            for chunk in _id_chunks(selected_ids):
                placeholders = ",".join("?" for _ in chunk)
                tag_rows.extend(
                    conn.execute(
                        "SELECT it.image_id, t.name, t.tag_type, t.category, "
                        "COALESCE(it.provenance, t.provenance, 'unknown') AS provenance "
                        "FROM image_tags it JOIN tags t ON t.id=it.tag_id "
                        f"WHERE it.image_id IN ({placeholders}) "
                        "ORDER BY it.image_id, t.name COLLATE NOCASE, t.name, "
                        "COALESCE(t.tag_type, t.category, 'unknown') COLLATE NOCASE, t.id",
                        chunk,
                    ).fetchall()
                )
        for tag_row in tag_rows:
            tags_by_image[int(tag_row["image_id"])].append(
                {
                    "name": str(tag_row["name"]),
                    "type": str(
                        tag_row["tag_type"] or tag_row["category"] or "unknown"
                    ),
                    "provenance": str(tag_row["provenance"]),
                }
            )

        updates: list[tuple[object, ...]] = []
        for image_row in image_rows:
            image_id = int(image_row["id"])
            tags = tags_by_image[image_id]
            rating = classify_content(image_row["purity"], tags)
            updates.append(
                (
                    rating.rating,
                    float(rating.confidence),
                    rating.basis,
                    json.dumps(
                        list(rating.reasons), ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    len(tags),
                    image_id,
                )
            )
        conn.executemany(
            "UPDATE images SET content_rating=?, rating_confidence=?, "
            "rating_basis=?, rating_reasons_json=?, tag_count=? WHERE id=?",
            updates,
        )
        facet_count = 0
        if refresh_facets:
            facet_count = _refresh_library_facets(conn, _now_iso())
        conn.execute("RELEASE SAVEPOINT refresh_derived_metadata")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT refresh_derived_metadata")
        conn.execute("RELEASE SAVEPOINT refresh_derived_metadata")
        raise
    return DerivedRefreshResult(images=len(image_rows), facets=facet_count)


def read_library_facets(
    conn: sqlite3.Connection,
) -> dict[str, tuple[FacetCount, ...]]:
    """Read the materialized facet snapshot without aggregating image tags."""
    grouped: dict[str, list[FacetCount]] = {}
    for row in conn.execute(
        "SELECT facet, value, count, refreshed_at FROM library_facets "
        "ORDER BY facet COLLATE NOCASE, count DESC, value COLLATE NOCASE, value"
    ):
        grouped.setdefault(str(row["facet"]), []).append(
            FacetCount(
                value=str(row["value"]), count=int(row["count"]),
                refreshed_at=str(row["refreshed_at"]),
            )
        )
    return {key: tuple(values) for key, values in grouped.items()}


def counted_tag_autocomplete(
    conn: sqlite3.Connection, prefix: str, limit: int = 20,
) -> list[TagAutocompleteResult]:
    """Return counted authoritative tag matches for a validated prefix."""
    if not isinstance(prefix, str):
        raise ValueError("prefix must be a string")
    prefix = prefix.strip()
    if not 1 <= len(prefix) <= 120:
        raise ValueError("prefix length must be from 1 through 120")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("limit must be from 1 through 50")
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = conn.execute(
        "SELECT t.name, COALESCE(t.tag_type, t.category, 'unknown') AS tag_type, "
        "COALESCE(it.provenance, t.provenance, 'unknown') AS provenance, "
        "COALESCE(t.source, i.source) AS source, "
        "COUNT(DISTINCT it.image_id) AS image_count "
        "FROM tags t JOIN image_tags it ON it.tag_id=t.id "
        "JOIN images i ON i.id=it.image_id "
        "WHERE t.name LIKE ? ESCAPE '\\' COLLATE NOCASE "
        "GROUP BY t.name, COALESCE(t.tag_type, t.category, 'unknown'), "
        "COALESCE(it.provenance, t.provenance, 'unknown'), "
        "COALESCE(t.source, i.source) "
        "ORDER BY image_count DESC, t.name COLLATE NOCASE, t.name, "
        "COALESCE(t.tag_type, t.category, 'unknown') COLLATE NOCASE, "
        "COALESCE(t.tag_type, t.category, 'unknown'), "
        "COALESCE(t.source, i.source) COLLATE NOCASE, "
        "COALESCE(it.provenance, t.provenance, 'unknown') "
        "LIMIT ?",
        (escaped + "%", limit),
    ).fetchall()
    return [
        TagAutocompleteResult(
            name=str(row["name"]), type=str(row["tag_type"]),
            provenance=str(row["provenance"]), source=str(row["source"]),
            image_count=int(row["image_count"]),
        )
        for row in rows
    ]


def _suggestion_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def list_tag_suggestions(
    conn: sqlite3.Connection,
    image_ids: Iterable[int],
    *,
    review_status: Optional[str] = None,
) -> dict[int, tuple[TagSuggestion, ...]]:
    """Batch-load suggestions for a page, grouped by image ID."""
    ids = _validated_image_ids(image_ids)
    if review_status is not None and review_status not in SUGGESTION_REVIEW_STATUSES:
        raise ValueError("review_status is invalid")
    grouped: dict[int, list[TagSuggestion]] = {image_id: [] for image_id in ids}
    for chunk in _id_chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            "SELECT * FROM tag_suggestions "
            f"WHERE image_id IN ({placeholders})"
        )
        params: list[object] = list(chunk)
        if review_status is not None:
            sql += " AND review_status=?"
            params.append(review_status)
        sql += (
            " ORDER BY image_id, review_status, normalized_label, generator, "
            "model_version, id"
        )
        for row in conn.execute(sql, params):
            suggestion = TagSuggestion.from_row(row)
            grouped[suggestion.image_id].append(suggestion)
    return {key: tuple(value) for key, value in grouped.items()}


def upsert_tag_suggestion(
    conn: sqlite3.Connection,
    *,
    image_id: int,
    label: str,
    confidence: float,
    generator: str,
    model_version: str,
    provenance: str,
) -> TagSuggestion:
    """Insert/update review-only evidence without changing authoritative tags."""
    ids = _validated_image_ids((image_id,))
    if conn.execute("SELECT 1 FROM images WHERE id=?", ids).fetchone() is None:
        raise ValueError(f"unknown image id: {image_id}")
    label = _suggestion_text(label, "label")
    normalized = normalize_label(label)
    if not normalized:
        raise ValueError("label must contain a visible word")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be a number from 0 through 1")
    confidence = float(confidence)
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a number from 0 through 1")
    generator = _suggestion_text(generator, "generator")
    model_version = _suggestion_text(model_version, "model_version")
    provenance = _suggestion_text(provenance, "provenance")
    conn.execute(
        "INSERT INTO tag_suggestions("
        "image_id,label,normalized_label,confidence,generator,model_version,"
        "provenance,review_status,created_at) VALUES (?,?,?,?,?,?,?,'pending',?) "
        "ON CONFLICT(image_id, normalized_label, generator, model_version) "
        "DO UPDATE SET label=excluded.label, confidence=excluded.confidence, "
        "provenance=excluded.provenance",
        (
            image_id, label, normalized, confidence, generator, model_version,
            provenance, _now_iso(),
        ),
    )
    row = conn.execute(
        "SELECT * FROM tag_suggestions WHERE image_id=? AND normalized_label=? "
        "AND generator=? AND model_version=?",
        (image_id, normalized, generator, model_version),
    ).fetchone()
    assert row is not None
    return TagSuggestion.from_row(row)


def review_tag_suggestion(
    conn: sqlite3.Connection,
    suggestion_id: int,
    *,
    review_status: str,
    reviewer: str,
    decision_note: Optional[str] = None,
) -> TagSuggestion:
    """Accept/reject a pending suggestion without promoting it to a tag."""
    if isinstance(suggestion_id, bool) or not isinstance(suggestion_id, int) or suggestion_id < 1:
        raise ValueError("suggestion_id must be a positive integer")
    if review_status not in {"accepted", "rejected"}:
        raise ValueError("review_status must be accepted or rejected")
    reviewer = _suggestion_text(reviewer, "reviewer")
    if decision_note is not None:
        if not isinstance(decision_note, str):
            raise ValueError("decision_note must be a string or null")
        decision_note = decision_note.strip() or None
    row = conn.execute(
        "SELECT * FROM tag_suggestions WHERE id=?", (suggestion_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown suggestion id: {suggestion_id}")
    current = str(row["review_status"])
    if current == review_status:
        return TagSuggestion.from_row(row)
    if current != "pending":
        raise ValueError(f"invalid suggestion transition: {current} -> {review_status}")
    cursor = conn.execute(
        "UPDATE tag_suggestions SET review_status=?, reviewed_at=?, reviewer=?, "
        "decision_note=? WHERE id=? AND review_status='pending'",
        (review_status, _now_iso(), reviewer, decision_note, suggestion_id),
    )
    if cursor.rowcount != 1:
        latest = conn.execute(
            "SELECT review_status FROM tag_suggestions WHERE id=?",
            (suggestion_id,),
        ).fetchone()
        latest_status = "missing" if latest is None else str(latest["review_status"])
        raise ValueError(
            "suggestion transition lost a concurrent decision: "
            f"pending -> {latest_status}"
        )
    updated = conn.execute(
        "SELECT * FROM tag_suggestions WHERE id=?", (suggestion_id,),
    ).fetchone()
    assert updated is not None
    return TagSuggestion.from_row(updated)


def default_wallhaven_ledger_path(library_root: Path) -> Path:
    """Return the durable Wallhaven enrichment ledger for a library root."""
    return Path(library_root) / WALLHAVEN_LEDGER_RELATIVE_PATH


def default_provider_ledger_path(library_root: Path) -> Path:
    """Return the durable generic provider-evidence ledger path."""
    return Path(library_root) / PROVIDER_LEDGER_RELATIVE_PATH


def _normalise_wallhaven_source_id(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip().casefold()


def _optional_text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalise_ledger_tag(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("tag must be an object")
    name = _optional_text(value.get("name"))
    if name is None:
        raise ValueError("tag.name must be a non-empty string")
    provenance = _optional_text(value.get("provenance"))
    if provenance != WALLHAVEN_LEDGER_PROVENANCE:
        raise ValueError("tag.provenance must be wallhaven-api")
    category = _optional_text(value.get("category"))
    tag_type = _optional_text(value.get("type")) or category or "unknown"
    slug = _optional_text(value.get("slug")) or _tag_slug(name)
    tag: dict[str, object] = {
        "name": name,
        "slug": slug,
        "type": tag_type,
        "provenance": WALLHAVEN_LEDGER_PROVENANCE,
    }
    category_id = value.get("category_id")
    if isinstance(category_id, int) and not isinstance(category_id, bool):
        tag["category_id"] = category_id
    if category is not None:
        tag["category"] = category
    return tag


def _normalise_wallhaven_ledger_record(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("record must be an object")
    if value.get("schema_version") != WALLHAVEN_LEDGER_SCHEMA_VERSION:
        raise ValueError("schema_version must be 1")
    if value.get("record_type") != WALLHAVEN_LEDGER_RECORD_TYPE:
        raise ValueError(
            f"record_type must be {WALLHAVEN_LEDGER_RECORD_TYPE!r}"
        )
    if value.get("source") != "wallhaven":
        raise ValueError("source must be wallhaven")
    source_id = _normalise_wallhaven_source_id(value.get("source_id"))
    if not source_id:
        raise ValueError("source_id must be a non-empty string")
    status = value.get("enrichment_status")
    if status not in {STATUS_PENDING, STATUS_OK, STATUS_SKIPPED, STATUS_FAILED}:
        raise ValueError("enrichment_status is invalid")
    if value.get("provenance") != WALLHAVEN_LEDGER_PROVENANCE:
        raise ValueError("provenance must be wallhaven-api")
    franchise = value.get("franchise")
    purity = value.get("purity")
    if franchise is not None and not isinstance(franchise, str):
        raise ValueError("franchise must be a string or null")
    if purity is not None and not isinstance(purity, str):
        raise ValueError("purity must be a string or null")
    raw_tags = value.get("tags")
    if not isinstance(raw_tags, list):
        raise ValueError("tags must be an array")
    tags = [_normalise_ledger_tag(tag) for tag in raw_tags]
    tags.sort(
        key=lambda tag: (
            str(tag["name"]).casefold(), str(tag["type"]).casefold(),
            str(tag.get("category") or "").casefold(),
        )
    )
    captured_at = value.get("captured_at")
    if captured_at is not None:
        captured_at = _normalise_captured_at(captured_at)
    error = value.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError("error must be a string or null")
    return {
        "schema_version": WALLHAVEN_LEDGER_SCHEMA_VERSION,
        "record_type": WALLHAVEN_LEDGER_RECORD_TYPE,
        "source": "wallhaven",
        "source_id": source_id,
        "enrichment_status": status,
        "franchise": _optional_text(franchise),
        "purity": _optional_text(purity),
        "provenance": WALLHAVEN_LEDGER_PROVENANCE,
        "tags": tags,
        "captured_at": captured_at,
        "error": _optional_text(error),
    }


def _jsonl_record_bytes(record: dict[str, object]) -> bytes:
    return (
        json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def load_wallhaven_ledger(
    ledger_path: Path,
) -> tuple[dict[str, dict[str, object]], dict[str, int]]:
    """Load a Wallhaven JSONL ledger; the last valid record per ID wins."""
    ledger_path = Path(ledger_path)
    records: dict[str, dict[str, object]] = {}
    stats = {
        "ledger_lines": 0,
        "ledger_records": 0,
        "ledger_invalid": 0,
        "ledger_superseded": 0,
    }
    if not ledger_path.is_file():
        return records, stats
    with ledger_path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            stats["ledger_lines"] += 1
            try:
                decoded = raw_line.decode("utf-8")
                record = _normalise_wallhaven_ledger_record(json.loads(decoded))
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                stats["ledger_invalid"] += 1
                logger.warning(
                    "ignoring invalid Wallhaven ledger record %s:%d: %s",
                    ledger_path, line_number, exc,
                )
                continue
            source_id = str(record["source_id"])
            if source_id in records:
                stats["ledger_superseded"] += 1
            records[source_id] = record
    stats["ledger_records"] = len(records)
    return records, stats


def _append_wallhaven_ledger_record(
    ledger_path: Path, record: dict[str, object],
) -> None:
    """Append and fsync one validated record before SQLite is committed."""
    record = _normalise_wallhaven_ledger_record(record)
    ledger_path = Path(ledger_path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _jsonl_record_bytes(record)
    with ledger_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() > 0:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) not in {b"\n", b"\r"}:
                handle.seek(0, os.SEEK_END)
                handle.write(b"\n")
        handle.seek(0, os.SEEK_END)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _normalise_captured_at(value: object) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError("captured_at must be a non-empty ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("captured_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("captured_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _normalise_provider_tag(
    value: object, *, provenance: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("provider tag must be an object")
    name = _optional_text(value.get("name"))
    tag_type = _optional_text(value.get("type"))
    if name is None:
        raise ValueError("provider tag.name must be a non-empty string")
    if tag_type is None:
        raise ValueError("provider tag.type must be a non-empty string")
    supplied_provenance = _optional_text(value.get("provenance"))
    if supplied_provenance is not None and supplied_provenance != provenance:
        raise ValueError("provider tag provenance must match its record")
    tag: dict[str, object] = {
        "name": name,
        "slug": _optional_text(value.get("slug")) or _tag_slug(name),
        "type": tag_type,
        "provenance": provenance,
    }
    category = _optional_text(value.get("category"))
    if category is not None:
        tag["category"] = category
    category_id = value.get("category_id")
    if category_id is not None:
        if isinstance(category_id, bool) or not isinstance(category_id, int):
            raise ValueError("provider tag.category_id must be an integer or null")
        tag["category_id"] = category_id
    if "raw" in value:
        raw = value["raw"]
        try:
            json.dumps(raw, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("provider tag.raw must be JSON serializable") from exc
        tag["raw"] = raw
    return tag


def normalize_provider_ledger_record(value: object) -> dict[str, object]:
    """Validate and normalize one generic captured-provider record."""
    if not isinstance(value, Mapping):
        raise ValueError("provider record must be an object")
    if value.get("schema_version") != PROVIDER_LEDGER_SCHEMA_VERSION:
        raise ValueError("provider record schema_version must be 1")
    if value.get("record_type") != PROVIDER_LEDGER_RECORD_TYPE:
        raise ValueError(
            f"provider record_type must be {PROVIDER_LEDGER_RECORD_TYPE!r}"
        )
    source = canonical_source(str(value.get("source") or ""))
    if source not in PROVIDER_LEDGER_SOURCES:
        raise ValueError(
            "provider source must be zerochan or anime-pictures"
        )
    source_id = _normalise_wallhaven_source_id(value.get("source_id"))
    if not source_id:
        raise ValueError("provider source_id must be a non-empty string")
    status = value.get("status")
    if status not in PROVIDER_LEDGER_STATUSES:
        raise ValueError("provider status is invalid")
    provenance = _optional_text(value.get("provenance"))
    if provenance is None:
        raise ValueError("provider provenance must be a non-empty string")
    raw_tags = value.get("tags")
    if not isinstance(raw_tags, list):
        raise ValueError("provider tags must be an array")
    tags = [
        _normalise_provider_tag(tag, provenance=provenance)
        for tag in raw_tags
    ]
    if status != STATUS_OK and tags:
        raise ValueError("only an ok provider record may contain tags")
    tags.sort(
        key=lambda tag: (
            str(tag["name"]).casefold(), str(tag["name"]),
            str(tag["type"]).casefold(), str(tag["type"]),
        )
    )
    error = value.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError("provider error must be a string or null")
    return {
        "schema_version": PROVIDER_LEDGER_SCHEMA_VERSION,
        "record_type": PROVIDER_LEDGER_RECORD_TYPE,
        "source": source,
        "source_id": source_id,
        "status": status,
        "tags": tags,
        "provenance": provenance,
        "captured_at": _normalise_captured_at(value.get("captured_at")),
        "error": _optional_text(error),
    }


def load_provider_ledger(
    ledger_path: Path,
) -> tuple[dict[tuple[str, str], dict[str, object]], dict[str, int]]:
    """Load generic provider JSONL; the last valid source identity wins."""
    path = Path(ledger_path)
    records: dict[tuple[str, str], dict[str, object]] = {}
    stats = {
        "provider_ledger_lines": 0,
        "provider_ledger_records": 0,
        "provider_ledger_invalid": 0,
        "provider_ledger_superseded": 0,
    }
    if not path.is_file():
        return records, stats
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            stats["provider_ledger_lines"] += 1
            try:
                record = normalize_provider_ledger_record(
                    json.loads(raw_line.decode("utf-8"))
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                stats["provider_ledger_invalid"] += 1
                logger.warning(
                    "ignoring invalid provider ledger record %s:%d: %s",
                    path, line_number, exc,
                )
                continue
            identity = (str(record["source"]), str(record["source_id"]))
            if identity in records:
                stats["provider_ledger_superseded"] += 1
            records[identity] = record
    stats["provider_ledger_records"] = len(records)
    return records, stats


def append_provider_ledger_record(
    ledger_path: Path, record: Mapping[str, object],
) -> dict[str, object]:
    """Append and fsync one strict generic provider record."""
    normalized = normalize_provider_ledger_record(record)
    path = Path(ledger_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _jsonl_record_bytes(normalized)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() > 0:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) not in {b"\n", b"\r"}:
                handle.seek(0, os.SEEK_END)
                handle.write(b"\n")
        handle.seek(0, os.SEEK_END)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    return normalized


def apply_provider_enrichment_records(
    conn: sqlite3.Connection,
    records: (
        Mapping[tuple[str, str], Mapping[str, object]]
        | Iterable[Mapping[str, object]]
    ),
    *,
    refresh_derived: bool = True,
) -> dict[str, int]:
    """Apply captured evidence by exact source identity and provenance."""
    values = records.values() if isinstance(records, Mapping) else records
    normalized_by_identity: dict[tuple[str, str], dict[str, object]] = {}
    for value in values:
        record = normalize_provider_ledger_record(value)
        normalized_by_identity[
            (str(record["source"]), str(record["source_id"]))
        ] = record
    stats = {
        "provider_records": len(normalized_by_identity),
        "provider_records_matched": 0,
        "provider_records_unmatched": 0,
        "provider_images_updated": 0,
        "provider_tags_applied": 0,
    }
    affected_ids: set[int] = set()
    for (source, source_id), record in sorted(normalized_by_identity.items()):
        image_rows = conn.execute(
            "SELECT id FROM images WHERE source=? "
            "AND LOWER(CAST(source_site_id AS TEXT))=? ORDER BY id",
            (source, source_id),
        ).fetchall()
        if not image_rows:
            stats["provider_records_unmatched"] += 1
            continue
        stats["provider_records_matched"] += 1
        provenance = str(record["provenance"])
        for image_row in image_rows:
            image_id = int(image_row["id"])
            affected_ids.add(image_id)
            conn.execute(
                "DELETE FROM image_tags WHERE image_id=? AND provenance=?",
                (image_id, provenance),
            )
            for tag in record["tags"]:
                assert isinstance(tag, dict)
                name = str(tag["name"])
                tag_type = str(tag["type"])
                tag_id = _stable_tag_id(source, name, tag_type)
                conn.execute(
                    "INSERT INTO tags(id,name,category_id,category,source,slug,"
                    "tag_type,provenance) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                    "category_id=COALESCE(excluded.category_id,tags.category_id), "
                    "category=COALESCE(excluded.category,tags.category), "
                    "source=excluded.source, slug=excluded.slug, "
                    "tag_type=excluded.tag_type, "
                    "provenance=COALESCE(tags.provenance,excluded.provenance)",
                    (
                        tag_id, name, tag.get("category_id"), tag.get("category"),
                        source, tag.get("slug") or _tag_slug(name), tag_type,
                        provenance,
                    ),
                )
                conn.execute(
                    "INSERT INTO image_tags(image_id,tag_id,provenance) "
                    "VALUES (?,?,?) ON CONFLICT(image_id,tag_id) DO NOTHING",
                    (image_id, tag_id, provenance),
                )
                stats["provider_tags_applied"] += 1
            conn.execute(
                "UPDATE images SET enrichment_status=? WHERE id=?",
                (record["status"], image_id),
            )
            stats["provider_images_updated"] += 1
    conn.execute(
        "DELETE FROM tags WHERE NOT EXISTS "
        "(SELECT 1 FROM image_tags WHERE image_tags.tag_id=tags.id)"
    )
    if refresh_derived:
        refresh_derived_metadata(conn, affected_ids)
    return stats


def import_provider_ledger(
    conn: sqlite3.Connection, ledger_path: Path,
) -> dict[str, int]:
    """Load and apply a generic provider ledger without any network access."""
    records, load_stats = load_provider_ledger(ledger_path)
    apply_stats = apply_provider_enrichment_records(conn, records)
    return {**load_stats, **apply_stats}


def provider_coverage(conn: sqlite3.Connection) -> dict[str, object]:
    """Return stable coverage groups from authoritative index evidence."""
    total = int(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0])
    by_source_status = [
        {
            "source": str(row["source"]),
            "status": str(row["enrichment_status"]),
            "count": int(row["count"]),
        }
        for row in conn.execute(
            "SELECT source,enrichment_status,COUNT(*) AS count FROM images "
            "GROUP BY source,enrichment_status "
            "ORDER BY source COLLATE NOCASE,enrichment_status"
        )
    ]
    bucket_sql = (
        "CASE WHEN tag_count=0 THEN '0' WHEN tag_count=1 THEN '1' "
        "WHEN tag_count BETWEEN 2 AND 4 THEN '2-4' "
        "WHEN tag_count BETWEEN 5 AND 9 THEN '5-9' ELSE '10+' END"
    )
    by_tag_count_bucket = [
        {
            "source": str(row["source"]), "bucket": str(row["bucket"]),
            "count": int(row["count"]),
        }
        for row in conn.execute(
            f"SELECT source,{bucket_sql} AS bucket,COUNT(*) AS count "
            f"FROM images GROUP BY source,{bucket_sql} "
            "ORDER BY source COLLATE NOCASE,bucket"
        )
    ]
    by_provenance = [
        {
            "source": str(row["source"]),
            "provenance": str(row["provenance"]),
            "image_count": int(row["image_count"]),
            "tag_count": int(row["tag_count"]),
        }
        for row in conn.execute(
            "SELECT COALESCE(t.source,i.source) AS source, "
            "COALESCE(it.provenance,t.provenance,'unknown') AS provenance, "
            "COUNT(DISTINCT it.image_id) AS image_count, COUNT(*) AS tag_count "
            "FROM image_tags it JOIN images i ON i.id=it.image_id "
            "JOIN tags t ON t.id=it.tag_id "
            "GROUP BY COALESCE(t.source,i.source), "
            "COALESCE(it.provenance,t.provenance,'unknown') "
            "ORDER BY COALESCE(t.source,i.source) COLLATE NOCASE, "
            "COALESCE(it.provenance,t.provenance,'unknown') COLLATE NOCASE"
        )
    ]
    return {
        "total_images": total,
        "by_source_status": by_source_status,
        "by_tag_count_bucket": by_tag_count_bucket,
        "by_provenance": by_provenance,
    }


def _legacy_status(value: object) -> str:
    status = _optional_text(value)
    if status in {STATUS_PENDING, STATUS_OK, STATUS_SKIPPED, STATUS_FAILED}:
        return status
    return STATUS_PENDING


def _open_sqlite_read_only(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"legacy SQLite database does not exist: {db_path}")
    conn = sqlite3.connect(
        f"{db_path.as_uri()}?mode=ro",
        uri=True,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only = ON")
        _register_index_sqlite_functions(conn)
    except Exception:
        conn.close()
        raise
    return conn


def open_index_read_only(db_path: Path) -> sqlite3.Connection:
    """Open an existing index in query-only mode with helper functions loaded."""
    conn = _open_sqlite_read_only(db_path)
    try:
        _validate_current_index_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _validate_current_index_schema(conn: sqlite3.Connection) -> None:
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if user_version != DB_SCHEMA_VERSION:
        raise ValueError(
            f"index schema version {user_version} is not {DB_SCHEMA_VERSION}"
        )
    tables = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for table, required in _VERIFY_REQUIRED_COLUMNS.items():
        if table not in tables:
            raise ValueError(f"index is missing required table: {table}")
        missing = required - _table_columns(conn, table)
        if missing:
            raise ValueError(
                f"index is missing required columns in {table}: "
                + ", ".join(sorted(missing))
            )
    marker = conn.execute(
        "SELECT value FROM schema_metadata WHERE key='schema_version'"
    ).fetchone()
    if marker is None or str(marker["value"]) != str(DB_SCHEMA_VERSION):
        value = None if marker is None else marker["value"]
        raise ValueError(
            f"schema_metadata.schema_version is {value!r}, "
            f"expected {DB_SCHEMA_VERSION}"
        )


def open_index_write(db_path: Path) -> sqlite3.Connection:
    """Open schema-3 in read/write mode without creating or migrating it."""
    database = Path(db_path).resolve()
    if not database.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {database}")
    conn = sqlite3.connect(
        f"{database.as_uri()}?mode=rw", uri=True,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
    )
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        if journal_mode.casefold() != SQLITE_JOURNAL_MODE:
            raise sqlite3.OperationalError(
                f"index journal mode is {journal_mode}, expected {SQLITE_JOURNAL_MODE}"
            )
        _register_index_sqlite_functions(conn)
        _validate_current_index_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _new_verification_counts() -> dict[str, int]:
    """Return all stable counters exposed by a library verification report."""
    return {
        "disk_images": 0,
        "indexed_images": 0,
        "missing_indexed_paths": 0,
        "outside_root_paths": 0,
        "unindexed_disk_images": 0,
        "quarantine_rows": 0,
        "missing_sidecars": 0,
        "invalid_sidecars": 0,
        "metadata_mismatches": 0,
        "layout_mismatches": 0,
        "duplicate_sha_groups": 0,
        "foreign_key_violations": 0,
        "quick_check_failures": 0,
        "schema_failures": 0,
        "derived_metadata_failures": 0,
        "facet_failures": 0,
        "suggestion_failures": 0,
        "unsupported_indexed_extensions": 0,
        "indexed_path_collisions": 0,
        "indexed_noncanonical_paths": 0,
        "invalid_sha256": 0,
        "outside_root_disk_images": 0,
        "disk_path_collisions": 0,
        "unreadable_images": 0,
        "ledger_lines": 0,
        "ledger_records": 0,
        "invalid_ledger_records": 0,
        "ledger_superseded": 0,
    }


def _verification_report(
    *,
    library_root: Path,
    db_path: Path,
    quick_check: list[str],
    counts: dict[str, int],
    issues_total: int,
    issues: list[dict[str, str]],
) -> dict[str, object]:
    ok = issues_total == 0
    return {
        "schema_version": VERIFY_REPORT_SCHEMA_VERSION,
        "ok": ok,
        "status": "ok" if ok else "failed",
        "library_root": str(library_root),
        "db_path": str(db_path),
        "quick_check": quick_check,
        "counts": counts,
        "issues_total": issues_total,
        "issues_truncated": issues_total > len(issues),
        "issues": issues,
    }


def _verification_input_error_report(
    db_path: Path, library_root: Path, detail: str,
) -> dict[str, object]:
    """Return the stable JSON shape for a verifier input/open failure."""
    issue = {
        "code": "input-error",
        "path": str(Path(db_path).resolve()),
        "detail": detail,
    }
    return _verification_report(
        library_root=Path(library_root).resolve(),
        db_path=Path(db_path).resolve(),
        quick_check=[],
        counts=_new_verification_counts(),
        issues_total=1,
        issues=[issue],
    )


def _verification_exit_code(report: dict[str, object]) -> int:
    """Map a verification report to the stable CLI exit-code contract."""
    if report.get("ok") is True:
        return 0
    counts = report.get("counts")
    if isinstance(counts, dict) and counts.get("schema_failures", 0):
        return 2
    issues = report.get("issues")
    if isinstance(issues, list) and any(
        isinstance(issue, dict)
        and issue.get("code") in _VERIFY_INCOMPATIBLE_CODES | {"input-error"}
        for issue in issues
    ):
        return 2
    return 1


def _print_verification_summary(report: dict[str, object]) -> None:
    """Print a concise human-readable verification result and issue samples."""
    counts = report.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    quick_check = report.get("quick_check")
    if not isinstance(quick_check, list):
        quick_check = []
    issues = report.get("issues")
    if not isinstance(issues, list):
        issues = []

    print(f"Verification: {report.get('status', 'failed')}")
    print(f"Library: {report.get('library_root', '')}")
    print(f"Database: {report.get('db_path', '')}")
    print(
        "Images: "
        f"disk={counts.get('disk_images', 0)} "
        f"indexed={counts.get('indexed_images', 0)}"
    )
    print(
        f"Issues: {report.get('issues_total', 0)}"
        + (" (details truncated)" if report.get("issues_truncated") else "")
    )
    print("SQLite quick_check: " + (", ".join(map(str, quick_check)) or "unavailable"))
    for issue in issues[:10]:
        if not isinstance(issue, dict):
            continue
        print(
            f"- {issue.get('code', 'unknown')}: {issue.get('path', '')}"
            f" — {issue.get('detail', '')}"
        )


def _verification_path_key(path: Path) -> str:
    """Return the platform-normalized absolute identity for a path."""
    return os.path.normcase(str(path.resolve()))


def _is_verification_excluded(relative_path: Path) -> bool:
    excluded = {"_exactduplicates", "_metadata"}
    return bool(excluded.intersection(part.casefold() for part in relative_path.parts))


def verify_library(db_path: Path, library_root: Path) -> dict[str, object]:
    """Return a JSON-serializable health report without mutating DB or library.

    Args:
        db_path: Existing SQLite index to open in read-only mode.
        library_root: Existing canonical library directory to inspect.

    Returns:
        A stable version-1 report with exact aggregate counters and at most 200
        deterministic issue records.

    Raises:
        FileNotFoundError: If the database or library root does not exist.
        NotADirectoryError: If ``library_root`` is not a directory.
        OSError: If an input cannot be read.
        sqlite3.Error: If SQLite cannot open or inspect the database.
    """
    root = Path(library_root).resolve(strict=True)
    if not root.is_dir():
        raise NotADirectoryError(f"library root is not a directory: {root}")
    database = Path(db_path).resolve()

    counts = _new_verification_counts()
    issues: list[dict[str, str]] = []
    issues_total = 0
    quick_check: list[str] = []

    def record_issue(code: str, path: Path | str, detail: str) -> None:
        nonlocal issues_total
        issues_total += 1
        if len(issues) < VERIFY_MAX_ISSUES:
            issues.append(
                {"code": code, "path": str(path), "detail": str(detail)}
            )

    conn = _open_sqlite_read_only(database)
    try:
        quick_check = [
            str(row[0]) for row in conn.execute("PRAGMA quick_check").fetchall()
        ]
        quick_failures = [
            value for value in quick_check if value.strip().casefold() != "ok"
        ]
        if not quick_check:
            counts["quick_check_failures"] += 1
            record_issue(
                "sqlite-quick-check", database,
                "PRAGMA quick_check returned no result",
            )
        for failure in quick_failures:
            counts["quick_check_failures"] += 1
            record_issue("sqlite-quick-check", database, failure)

        foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        counts["foreign_key_violations"] = len(foreign_key_rows)
        for row in foreign_key_rows:
            record_issue(
                "foreign-key-violation",
                database,
                (
                    f"table={row['table']}; rowid={row['rowid']}; "
                    f"parent={row['parent']}; fkid={row['fkid']}"
                ),
            )

        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        structurally_compatible = True
        for table in sorted(_VERIFY_REQUIRED_COLUMNS):
            if table not in tables:
                structurally_compatible = False
                counts["schema_failures"] += 1
                record_issue(
                    "missing-table", database,
                    f"required table is missing: {table}",
                )
                continue
            columns = _table_columns(conn, table)
            for column in sorted(_VERIFY_REQUIRED_COLUMNS[table] - columns):
                structurally_compatible = False
                counts["schema_failures"] += 1
                record_issue(
                    "missing-column", database,
                    f"required column is missing: {table}.{column}",
                )

        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version != DB_SCHEMA_VERSION:
            counts["schema_failures"] += 1
            record_issue(
                "schema-version-mismatch", database,
                f"PRAGMA user_version={user_version}; expected {DB_SCHEMA_VERSION}",
            )
        if (
            "schema_metadata" in tables
            and {"key", "value"}.issubset(_table_columns(conn, "schema_metadata"))
        ):
            schema_row = conn.execute(
                "SELECT value FROM schema_metadata WHERE key='schema_version'"
            ).fetchone()
            metadata_version = None if schema_row is None else str(schema_row["value"])
            if metadata_version != str(DB_SCHEMA_VERSION):
                counts["schema_failures"] += 1
                record_issue(
                    "schema-version-mismatch", database,
                    "schema_metadata.schema_version="
                    f"{metadata_version!r}; expected {DB_SCHEMA_VERSION}",
                )

        if structurally_compatible:
            derived_rows = conn.execute(
                "SELECT id,purity,content_rating,rating_confidence,rating_basis,"
                "rating_reasons_json,tag_count FROM images ORDER BY id"
            ).fetchall()
            tags_by_image: dict[int, list[dict[str, str]]] = {
                int(row["id"]): [] for row in derived_rows
            }
            for tag_row in conn.execute(
                "SELECT it.image_id,t.name FROM image_tags it "
                "JOIN tags t ON t.id=it.tag_id "
                "ORDER BY it.image_id,t.name COLLATE NOCASE,t.name,t.id"
            ):
                tags_by_image.setdefault(int(tag_row["image_id"]), []).append(
                    {"name": str(tag_row["name"])}
                )
            for row in derived_rows:
                image_id = int(row["id"])
                rating = classify_content(row["purity"], tags_by_image[image_id])
                try:
                    reasons = json.loads(row["rating_reasons_json"])
                    valid_reasons = (
                        isinstance(reasons, list)
                        and all(isinstance(reason, str) for reason in reasons)
                    )
                except (TypeError, json.JSONDecodeError):
                    reasons = None
                    valid_reasons = False
                mismatches: list[str] = []
                if row["content_rating"] != rating.rating:
                    mismatches.append("content_rating")
                confidence = row["rating_confidence"]
                if (
                    not isinstance(confidence, (int, float))
                    or isinstance(confidence, bool)
                    or not 0 <= float(confidence) <= 1
                    or float(confidence) != float(rating.confidence)
                ):
                    mismatches.append("rating_confidence")
                if row["rating_basis"] != rating.basis:
                    mismatches.append("rating_basis")
                if not valid_reasons or reasons != list(rating.reasons):
                    mismatches.append("rating_reasons_json")
                if row["tag_count"] != len(tags_by_image[image_id]):
                    mismatches.append("tag_count")
                if mismatches:
                    counts["derived_metadata_failures"] += 1
                    record_issue(
                        "derived-metadata-mismatch", database,
                        f"image_id={image_id}; fields={','.join(mismatches)}",
                    )

            suggestion_rows = conn.execute(
                "SELECT * FROM tag_suggestions ORDER BY id"
            ).fetchall()
            for row in suggestion_rows:
                problems: list[str] = []
                if row["review_status"] not in SUGGESTION_REVIEW_STATUSES:
                    problems.append("review_status")
                confidence = row["confidence"]
                if (
                    not isinstance(confidence, (int, float))
                    or isinstance(confidence, bool)
                    or not 0 <= float(confidence) <= 1
                ):
                    problems.append("confidence")
                for field_name in (
                    "label", "normalized_label", "generator",
                    "model_version", "provenance", "created_at",
                ):
                    if not _optional_text(row[field_name]):
                        problems.append(field_name)
                if row["review_status"] == "pending":
                    if any(
                        row[field_name] is not None
                        for field_name in ("reviewed_at", "reviewer")
                    ):
                        problems.append("pending-review-fields")
                elif not row["reviewed_at"] or not _optional_text(row["reviewer"]):
                    problems.append("terminal-review-fields")
                if problems:
                    counts["suggestion_failures"] += 1
                    record_issue(
                        "invalid-tag-suggestion", database,
                        f"suggestion_id={row['id']}; fields={','.join(problems)}",
                    )

            stored_facets = {
                (str(row["facet"]), str(row["value"])): int(row["count"])
                for row in conn.execute(
                    "SELECT facet,value,count FROM library_facets"
                )
            }
            expected_facets: dict[tuple[str, str], int] = {}
            direct_facets = (
                ("content_rating", "content_rating"),
                ("source", "source"),
                (
                    "orientation",
                    "COALESCE(NULLIF(TRIM(orientation), ''), 'unknown')",
                ),
                (
                    "resolution_bucket",
                    "COALESCE(NULLIF(TRIM(resolution_bucket), ''), 'unknown')",
                ),
                ("enrichment_status", "enrichment_status"),
            )
            for facet, expression in direct_facets:
                for row in conn.execute(
                    f"SELECT {expression} AS value,COUNT(*) AS count "
                    f"FROM images GROUP BY {expression}"
                ):
                    expected_facets[(facet, str(row["value"]))] = int(row["count"])
            tag_bucket = (
                "CASE WHEN tag_count=0 THEN '0' WHEN tag_count=1 THEN '1' "
                "WHEN tag_count BETWEEN 2 AND 4 THEN '2-4' "
                "WHEN tag_count BETWEEN 5 AND 9 THEN '5-9' ELSE '10+' END"
            )
            for row in conn.execute(
                f"SELECT {tag_bucket} AS value,COUNT(*) AS count FROM images "
                f"GROUP BY {tag_bucket}"
            ):
                expected_facets[("tag_count_bucket", str(row["value"]))] = int(
                    row["count"]
                )
            for row in conn.execute(
                "SELECT source || '|' || enrichment_status AS value, "
                "COUNT(*) AS count FROM images GROUP BY source,enrichment_status"
            ):
                expected_facets[("provider_coverage", str(row["value"]))] = int(
                    row["count"]
                )
            for row in conn.execute(
                "SELECT COALESCE(t.source,i.source) || '|' || "
                "COALESCE(it.provenance,t.provenance,'unknown') AS value, "
                "COUNT(DISTINCT it.image_id) AS count FROM image_tags it "
                "JOIN images i ON i.id=it.image_id JOIN tags t ON t.id=it.tag_id "
                "GROUP BY COALESCE(t.source,i.source), "
                "COALESCE(it.provenance,t.provenance,'unknown')"
            ):
                expected_facets[("tag_provenance", str(row["value"]))] = int(
                    row["count"]
                )
            for key in sorted(set(stored_facets) | set(expected_facets)):
                if stored_facets.get(key) == expected_facets.get(key):
                    continue
                counts["facet_failures"] += 1
                record_issue(
                    "facet-mismatch", database,
                    f"facet={key[0]}; value={key[1]}; "
                    f"stored={stored_facets.get(key)}; "
                    f"expected={expected_facets.get(key)}",
                )

            disk_entries: list[Path] = []
            for entry in root.rglob("*"):
                if not entry.is_file() or entry.suffix.casefold() not in IMAGE_EXTENSIONS:
                    continue
                relative = entry.relative_to(root)
                if _is_verification_excluded(relative):
                    continue
                resolved_entry = entry.resolve(strict=True)
                try:
                    resolved_entry.relative_to(root)
                except ValueError:
                    counts["outside_root_disk_images"] += 1
                    record_issue(
                        "outside-root-disk-image", resolved_entry,
                        "supported image resolves outside the requested library root",
                    )
                    continue
                disk_entries.append(resolved_entry)
            disk_entries.sort(key=lambda path: (str(path).casefold(), str(path)))
            counts["disk_images"] = len(disk_entries)

            disk_by_key: dict[str, Path] = {}
            for entry in disk_entries:
                key = _verification_path_key(entry)
                if key in disk_by_key:
                    counts["disk_path_collisions"] += 1
                    record_issue(
                        "disk-path-collision", entry,
                        f"resolves to the same canonical path as {disk_by_key[key]}",
                    )
                else:
                    disk_by_key[key] = entry

            _, ledger_stats = load_wallhaven_ledger(
                default_wallhaven_ledger_path(root)
            )
            counts["ledger_lines"] = ledger_stats["ledger_lines"]
            counts["ledger_records"] = ledger_stats["ledger_records"]
            counts["invalid_ledger_records"] = ledger_stats["ledger_invalid"]
            counts["ledger_superseded"] = ledger_stats["ledger_superseded"]

            rows = conn.execute(
                "SELECT * FROM images ORDER BY path COLLATE NOCASE, path, id"
            ).fetchall()
            counts["indexed_images"] = len(rows)
            rows_by_disk_key: dict[str, list[sqlite3.Row]] = {}
            canonical_rows: list[tuple[Path, sqlite3.Row]] = []

            for row in rows:
                raw_path = str(row["path"])
                try:
                    indexed_path = Path(raw_path).resolve()
                except (OSError, RuntimeError, ValueError) as exc:
                    counts["missing_indexed_paths"] += 1
                    record_issue(
                        "missing-indexed-path", raw_path,
                        f"indexed path could not be resolved: {exc}",
                    )
                    continue

                try:
                    relative = indexed_path.relative_to(root)
                except ValueError:
                    counts["outside_root_paths"] += 1
                    record_issue(
                        "outside-root-path", indexed_path,
                        "indexed path resolves outside the requested library root",
                    )
                    relative = None

                exists = indexed_path.is_file()
                if not exists:
                    counts["missing_indexed_paths"] += 1
                    record_issue(
                        "missing-indexed-path", indexed_path,
                        "indexed image does not exist as a file",
                    )
                if relative is None:
                    continue
                relative_parts = {part.casefold() for part in relative.parts}
                if "_exactduplicates" in relative_parts:
                    counts["quarantine_rows"] += 1
                    record_issue(
                        "indexed-quarantine-file", indexed_path,
                        "_ExactDuplicates files must not appear in images",
                    )
                    continue
                if indexed_path.suffix.casefold() not in IMAGE_EXTENSIONS:
                    counts["unsupported_indexed_extensions"] += 1
                    record_issue(
                        "unsupported-indexed-extension", indexed_path,
                        f"unsupported extension: {indexed_path.suffix or '<none>'}",
                    )
                    continue

                canonical_rows.append((indexed_path, row))
                if not exists:
                    continue
                key = _verification_path_key(indexed_path)
                if key in disk_by_key:
                    rows_by_disk_key.setdefault(key, []).append(row)
                else:
                    counts["indexed_noncanonical_paths"] += 1
                    record_issue(
                        "indexed-noncanonical-path", indexed_path,
                        "indexed file is excluded from the canonical disk scan",
                    )

            for key, entry in sorted(
                disk_by_key.items(), key=lambda item: (str(item[1]).casefold(), str(item[1]))
            ):
                matching_rows = rows_by_disk_key.get(key, [])
                if not matching_rows:
                    counts["unindexed_disk_images"] += 1
                    record_issue(
                        "unindexed-disk-image", entry,
                        "canonical disk image has no matching images row",
                    )
                elif len(matching_rows) > 1:
                    counts["indexed_path_collisions"] += len(matching_rows) - 1
                    record_issue(
                        "indexed-path-collision", entry,
                        f"{len(matching_rows)} rows resolve to this canonical image",
                    )

            for image in disk_entries:
                image_key = _verification_path_key(image)
                matching_rows = rows_by_disk_key.get(image_key, [])
                indexed_row = matching_rows[0] if matching_rows else None
                sidecar = sidecar_path_for(image)
                if not sidecar.is_file():
                    counts["missing_sidecars"] += 1
                    record_issue(
                        "missing-sidecar", sidecar,
                        "canonical image has no sibling .wallpaper.json sidecar",
                    )
                    continue
                try:
                    document = load_metadata(sidecar)
                except MetadataValidationError as exc:
                    counts["invalid_sidecars"] += 1
                    record_issue("invalid-sidecar", sidecar, "; ".join(exc.errors))
                    continue

                file_metadata = document["file"]
                classification = document["classification"]
                mismatches: list[str] = []

                derived_bucket = (
                    resolution_bucket_for(
                        file_metadata["width"], file_metadata["height"],
                    )
                    or "_UnknownResolution"
                )
                derived_orientation = (
                    derive_orientation(
                        file_metadata["width"], file_metadata["height"],
                    )
                    or UNKNOWN_ORIENTATION
                )
                if classification["resolution_bucket"] != derived_bucket:
                    mismatches.append("sidecar resolution_bucket vs dimensions")
                if classification["orientation"] != derived_orientation:
                    mismatches.append("sidecar orientation vs dimensions")

                if document["canonical_filename"] != image.name:
                    mismatches.append("sidecar canonical_filename vs sibling filename")
                parsed_image = parse_canonical_filename(image.name)
                if parsed_image is None:
                    mismatches.append("sibling filename is not canonical v1")
                else:
                    comparisons = (
                        (document["source"], parsed_image.source, "source vs filename"),
                        (document["source_id"], parsed_image.source_id, "source_id vs filename"),
                        (file_metadata["extension"], parsed_image.extension, "extension vs filename"),
                        (file_metadata["width"], parsed_image.width, "width vs filename"),
                        (file_metadata["height"], parsed_image.height, "height vs filename"),
                    )
                    mismatches.extend(
                        label for actual, expected, label in comparisons
                        if actual != expected
                    )
                if file_metadata["extension"] != image.suffix:
                    mismatches.append("sidecar extension vs sibling file")
                try:
                    actual_size = image.stat().st_size
                except OSError as exc:
                    counts["unreadable_images"] += 1
                    record_issue("unreadable-image", image, str(exc))
                else:
                    if file_metadata["size_bytes"] != actual_size:
                        mismatches.append("sidecar size_bytes vs sibling file")

                if indexed_row is not None:
                    stored_metadata_path = indexed_row["metadata_path"]
                    if not stored_metadata_path:
                        mismatches.append("database metadata_path is empty")
                    else:
                        try:
                            stored_key = _verification_path_key(Path(stored_metadata_path))
                        except (OSError, RuntimeError, ValueError):
                            stored_key = ""
                        if stored_key != _verification_path_key(sidecar):
                            mismatches.append("database metadata_path vs sibling sidecar")
                    row_comparisons = (
                        (indexed_row["filename"], image.name, "database filename vs sibling file"),
                        (
                            indexed_row["canonical_filename"],
                            document["canonical_filename"],
                            "database canonical_filename vs sidecar",
                        ),
                        (indexed_row["source"], document["source"], "database source vs sidecar"),
                        (
                            indexed_row["source_site_id"], document["source_id"],
                            "database source_site_id vs sidecar",
                        ),
                        (indexed_row["ext"], file_metadata["extension"], "database ext vs sidecar"),
                        (indexed_row["width"], file_metadata["width"], "database width vs sidecar"),
                        (indexed_row["height"], file_metadata["height"], "database height vs sidecar"),
                        (
                            indexed_row["resolution_bucket"],
                            classification["resolution_bucket"],
                            "database resolution_bucket vs sidecar",
                        ),
                        (
                            indexed_row["orientation"], classification["orientation"],
                            "database orientation vs sidecar",
                        ),
                        (
                            str(indexed_row["sha256"] or "").casefold(),
                            str(file_metadata["sha256"]).casefold(),
                            "database sha256 vs sidecar",
                        ),
                        (
                            indexed_row["size_bytes"], file_metadata["size_bytes"],
                            "database size_bytes vs sidecar",
                        ),
                    )
                    mismatches.extend(
                        label for actual, expected, label in row_comparisons
                        if actual != expected
                    )

                if mismatches:
                    counts["metadata_mismatches"] += 1
                    record_issue(
                        "metadata-mismatch", image,
                        "; ".join(dict.fromkeys(mismatches)),
                    )

                actual_parent = image.relative_to(root).parts[:-1]
                expected_parent = (
                    classification["resolution_bucket"],
                    classification["orientation"],
                    document["source"],
                )
                if actual_parent != expected_parent:
                    counts["layout_mismatches"] += 1
                    record_issue(
                        "layout-mismatch", image,
                        "relative parent is "
                        f"{'/'.join(actual_parent) or '<root>'}; expected "
                        f"{'/'.join(expected_parent)}",
                    )

            sha_groups: dict[str, list[str]] = {}
            for indexed_path, row in canonical_rows:
                sha_value = row["sha256"]
                if not isinstance(sha_value, str) or _SHA256_RE.fullmatch(sha_value) is None:
                    counts["invalid_sha256"] += 1
                    record_issue(
                        "invalid-sha256", indexed_path,
                        "indexed canonical row requires a nonempty 64-hex SHA-256",
                    )
                    continue
                sha_groups.setdefault(sha_value.casefold(), []).append(str(indexed_path))

            for sha256, paths in sorted(sha_groups.items()):
                paths.sort(key=lambda value: (value.casefold(), value))
                if len(paths) < 2:
                    continue
                counts["duplicate_sha_groups"] += 1
                sample = paths[:5]
                remainder = len(paths) - len(sample)
                detail = f"sha256={sha256}; paths=" + " | ".join(sample)
                if remainder:
                    detail += f" | ... ({remainder} more)"
                record_issue("duplicate-sha256", sample[0], detail)
    finally:
        conn.close()

    return _verification_report(
        library_root=root,
        db_path=database,
        quick_check=quick_check,
        counts=counts,
        issues_total=issues_total,
        issues=issues,
    )


def _atomic_write_jsonl(
    output_path: Path, records: Iterable[dict[str, object]],
) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output_path.parent,
            prefix=f".{output_path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            for record in records:
                handle.write(_jsonl_record_bytes(record))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output_path)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def export_wallhaven_ledger(
    legacy_db_path: Path, output_path: Path,
) -> dict[str, int]:
    """Export legacy Wallhaven API metadata without mutating the source DB.

    Duplicate legacy image rows sharing a source ID are collapsed. The most
    complete status wins (ok, skipped, failed, pending), scalar fields come
    from the highest-ranked row, and tags are deterministically unioned.
    """
    legacy_db_path = Path(legacy_db_path).resolve()
    output_path = Path(output_path).resolve()
    if legacy_db_path == output_path:
        raise ValueError("ledger output path must differ from the SQLite database")
    conn = _open_sqlite_read_only(legacy_db_path)
    try:
        tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "images" not in tables:
            raise ValueError("legacy database has no images table")
        image_columns = _table_columns(conn, "images")
        required = {"id", "source", "source_site_id"}
        missing = required - image_columns
        if missing:
            raise ValueError(
                "legacy images table is missing: " + ", ".join(sorted(missing))
            )
        scalar_expr = {
            name: name if name in image_columns else f"NULL AS {name}"
            for name in ("enrichment_status", "franchise", "purity")
        }
        image_rows = conn.execute(
            "SELECT id, source_site_id, "
            f"{scalar_expr['enrichment_status']}, "
            f"{scalar_expr['franchise']}, {scalar_expr['purity']} "
            "FROM images WHERE LOWER(source)='wallhaven' "
            "AND source_site_id IS NOT NULL "
            "ORDER BY LOWER(CAST(source_site_id AS TEXT)), id"
        ).fetchall()

        grouped_rows: dict[str, list[sqlite3.Row]] = {}
        for row in image_rows:
            source_id = _normalise_wallhaven_source_id(row["source_site_id"])
            if source_id:
                grouped_rows.setdefault(source_id, []).append(row)

        grouped_tags: dict[str, list[dict[str, object]]] = {
            source_id: [] for source_id in grouped_rows
        }
        if {"image_tags", "tags"}.issubset(tables):
            tag_columns = _table_columns(conn, "tags")
            image_tag_columns = _table_columns(conn, "image_tags")
            if "name" in tag_columns:
                tag_expr = {
                    "slug": "t.slug" if "slug" in tag_columns else "NULL",
                    "tag_type": (
                        "t.tag_type" if "tag_type" in tag_columns else "NULL"
                    ),
                    "category_id": (
                        "t.category_id" if "category_id" in tag_columns else "NULL"
                    ),
                    "category": (
                        "t.category" if "category" in tag_columns else "NULL"
                    ),
                }
                if (
                    "provenance" in image_tag_columns
                    and "provenance" in tag_columns
                ):
                    provenance_filter = (
                        "AND COALESCE(it.provenance, t.provenance)="
                        "'wallhaven-api' "
                    )
                elif "provenance" in image_tag_columns:
                    provenance_filter = "AND it.provenance='wallhaven-api' "
                elif "provenance" in tag_columns:
                    provenance_filter = "AND t.provenance='wallhaven-api' "
                else:
                    # The original legacy schema had no provenance columns;
                    # all Wallhaven links in that schema came from the API.
                    provenance_filter = ""
                tag_rows = conn.execute(
                    "SELECT i.source_site_id, t.name, "
                    f"{tag_expr['slug']} AS slug, "
                    f"{tag_expr['tag_type']} AS tag_type, "
                    f"{tag_expr['category_id']} AS category_id, "
                    f"{tag_expr['category']} AS category "
                    "FROM images i "
                    "JOIN image_tags it ON it.image_id=i.id "
                    "JOIN tags t ON t.id=it.tag_id "
                    "WHERE LOWER(i.source)='wallhaven' "
                    "AND i.source_site_id IS NOT NULL "
                    f"{provenance_filter}"
                    "ORDER BY LOWER(CAST(i.source_site_id AS TEXT)), "
                    "LOWER(t.name), t.id"
                ).fetchall()
                for row in tag_rows:
                    source_id = _normalise_wallhaven_source_id(
                        row["source_site_id"]
                    )
                    if source_id not in grouped_tags:
                        continue
                    grouped_tags[source_id].append(
                        _normalise_ledger_tag(
                            {
                                "name": row["name"],
                                "slug": row["slug"],
                                "type": row["tag_type"] or row["category"],
                                "category_id": row["category_id"],
                                "category": row["category"],
                                "provenance": WALLHAVEN_LEDGER_PROVENANCE,
                            }
                        )
                    )

        status_rank = {
            STATUS_PENDING: 0, STATUS_FAILED: 1,
            STATUS_SKIPPED: 2, STATUS_OK: 3,
        }
        records: list[dict[str, object]] = []
        for source_id in sorted(grouped_rows, key=str.casefold):
            rows = sorted(
                grouped_rows[source_id],
                key=lambda row: (
                    -status_rank[_legacy_status(row["enrichment_status"])],
                    int(row["id"]),
                ),
            )
            status = _legacy_status(rows[0]["enrichment_status"])

            def first_scalar(name: str) -> Optional[str]:
                for candidate in rows:
                    value = _optional_text(candidate[name])
                    if value is not None:
                        return value
                return None

            merged_tags: dict[tuple[str, str], dict[str, object]] = {}
            for tag in grouped_tags[source_id]:
                key = (str(tag["name"]).casefold(), str(tag["type"]).casefold())
                current = merged_tags.get(key)
                if current is None:
                    merged_tags[key] = tag
                    continue
                candidates = (current, tag)
                merged_tags[key] = max(
                    candidates,
                    key=lambda candidate: (
                        sum(value not in {None, ""} for value in candidate.values()),
                        json.dumps(candidate, ensure_ascii=False, sort_keys=True),
                    ),
                )
            tags = sorted(
                merged_tags.values(),
                key=lambda tag: (
                    str(tag["name"]).casefold(), str(tag["type"]).casefold(),
                ),
            )
            records.append(
                _normalise_wallhaven_ledger_record(
                    {
                        "schema_version": WALLHAVEN_LEDGER_SCHEMA_VERSION,
                        "record_type": WALLHAVEN_LEDGER_RECORD_TYPE,
                        "source": "wallhaven",
                        "source_id": source_id,
                        "enrichment_status": status,
                        "franchise": first_scalar("franchise"),
                        "purity": first_scalar("purity"),
                        "provenance": WALLHAVEN_LEDGER_PROVENANCE,
                        "tags": tags,
                    }
                )
            )
    finally:
        conn.close()

    _atomic_write_jsonl(output_path, records)
    return {
        "source_rows": len(image_rows),
        "records": len(records),
        "duplicates_merged": len(image_rows) - len(records),
        "tags": sum(len(record["tags"]) for record in records),
    }


def ingest_library(
    conn: sqlite3.Connection,
    library_root: Path,
    ledger_path: Optional[Path] = None,
    provider_ledger_path: Optional[Path] = None,
) -> dict[str, int]:
    """Recursively rebuild the index view of one canonical library root.

    Valid metadata sidecars are authoritative.  Missing or invalid sidecars
    fall back to the legacy filename, header, and directory conventions.  A
    successful scan also removes rows for files that disappeared from this
    root, while leaving rows belonging to other roots untouched.
    """
    library_root = library_root.resolve()
    if not library_root.exists():
        raise FileNotFoundError(f"library root does not exist: {library_root}")
    if not library_root.is_dir():
        raise NotADirectoryError(f"library root is not a directory: {library_root}")
    if ledger_path is None:
        ledger_path = default_wallhaven_ledger_path(library_root)
    ledger_records, ledger_stats = load_wallhaven_ledger(ledger_path)
    if provider_ledger_path is None:
        provider_ledger_path = default_provider_ledger_path(library_root)
    provider_records, provider_ledger_stats = load_provider_ledger(
        provider_ledger_path
    )
    entries = sorted(
        (
            entry for entry in library_root.rglob("*")
            if entry.is_file()
            and entry.suffix.lower() in IMAGE_EXTENSIONS
            and "_exactduplicates" not in {
                part.casefold() for part in entry.relative_to(library_root).parts
            }
        ),
        key=lambda entry: str(entry).casefold(),
    )
    stats = {
        "scanned": 0,
        "wallhaven": 0,
        "zerochan": 0,
        "anime_pictures": 0,
        "unknown": 0,
        "tagged": 0,
        "sidecars": 0,
        "invalid_sidecars": 0,
        "stale_removed": 0,
        **ledger_stats,
        "ledger_applied": 0,
        "ledger_unmatched": 0,
        **provider_ledger_stats,
        "provider_records_matched": 0,
        "provider_records_unmatched": 0,
        "provider_images_updated": 0,
        "provider_tags_applied": 0,
    }
    now = _now_iso()
    scanned_paths: set[str] = set()
    applied_ledger_ids: set[str] = set()

    for entry in entries:
        entry = entry.resolve()
        path_text = str(entry)
        scanned_paths.add(os.path.normcase(path_text))
        relative_path = entry.relative_to(library_root)
        stats["scanned"] += 1

        metadata = None
        metadata_path = sidecar_path_for(entry)
        if metadata_path.is_file():
            try:
                metadata = load_metadata(metadata_path)
                integrity_errors: list[str] = []
                if metadata["canonical_filename"] != entry.name:
                    integrity_errors.append(
                        "canonical_filename does not match the sibling image"
                    )
                try:
                    actual_size = entry.stat().st_size
                except OSError as exc:
                    integrity_errors.append(f"could not stat sibling image: {exc}")
                else:
                    if metadata["file"]["size_bytes"] != actual_size:
                        integrity_errors.append(
                            "file.size_bytes does not match the sibling image"
                        )
                if integrity_errors:
                    raise MetadataValidationError(integrity_errors)
                stats["sidecars"] += 1
            except MetadataValidationError as exc:
                metadata = None
                stats["invalid_sidecars"] += 1
                logger.warning("ignoring invalid sidecar %s: %s", metadata_path, exc)

        parsed_canonical = parse_canonical_filename(entry.name)
        cls = classify_filename(entry.name if parsed_canonical else entry.stem)
        source_hint = _source_from_relative_path(relative_path)
        if cls.source == "unknown" and source_hint != "unknown":
            cls = Classification(
                source=source_hint,
                source_site_id=cls.source_site_id,
                width=cls.width,
                height=cls.height,
                tag_suffix=cls.tag_suffix,
            )

        tag_records: list[dict[str, str]] = []
        franchise = None
        if metadata is not None:
            source = metadata["source"]
            source_site_id = metadata["source_id"]
            width = metadata["file"]["width"]
            height = metadata["file"]["height"]
            orientation = metadata["classification"]["orientation"]
            bucket_name = metadata["classification"]["resolution_bucket"]
            ext = metadata["file"]["extension"]
            tag_records = [dict(tag) for tag in metadata["tags"]]
            for tag in tag_records:
                if tag["type"].casefold() in {"franchise", "series", "copyright"}:
                    franchise = tag["name"]
                    break
            source_url = metadata["source_url"]
            original_filename = metadata["original_filename"]
            canonical_filename = metadata["canonical_filename"]
            slug = metadata["slug"]
            sha256 = metadata["file"]["sha256"].lower()
            size_bytes = metadata["file"]["size_bytes"]
            transport = metadata["download"]["transport"]
            source_relative_path = metadata["download"]["source_relative_path"]
            download_recorded_at = metadata["download"]["recorded_at"]
            search_origins_json = json.dumps(
                metadata["search_origins"], ensure_ascii=False, separators=(",", ":"),
            )
        else:
            source = canonical_source(cls.source)
            source_site_id = cls.source_site_id
            width, height = cls.width, cls.height
            if width is None or height is None:
                width, height = read_image_dimensions(entry)
            width = width or 0
            height = height or 0
            orientation = derive_orientation(width, height) or UNKNOWN_ORIENTATION
            if width == 0 or height == 0:
                bucket_name = "_UnknownResolution"
            else:
                bucket_name = (
                    _bucket_from_relative_path(relative_path)
                    or resolution_bucket_for(width, height)
                    or "_UnknownResolution"
                )
            ext = entry.suffix.lower()
            if source == "anime-pictures" and cls.tag_suffix:
                parsed_tags = parse_anime_pictures_tags(cls.tag_suffix)
                franchise = parsed_tags.franchise
                names = ([franchise] if franchise else []) + parsed_tags.tags
                tag_records = [
                    {
                        "name": name,
                        "slug": _tag_slug(name),
                        "type": "unknown",
                        "provenance": "legacy-filename",
                    }
                    for name in names if name
                ]
            source_url = None
            original_filename = entry.name
            canonical_filename = parsed_canonical.filename if parsed_canonical else None
            slug = parsed_canonical.slug if parsed_canonical else None
            sha256 = None
            try:
                size_bytes = entry.stat().st_size
            except OSError:
                size_bytes = None
            transport = None
            source_relative_path = relative_path.as_posix()
            download_recorded_at = None
            search_origins_json = "[]"

        enrichment_status = {
            # Sidecar tags may be staging/search context; only the API pass can
            # declare Wallhaven enrichment complete.
            "wallhaven": STATUS_PENDING,
            "zerochan": STATUS_OK if metadata is not None else STATUS_SKIPPED,
            "anime-pictures": STATUS_OK,
            "unknown": STATUS_SKIPPED,
        }[source]
        values = {
            "path": path_text,
            "filename": entry.name,
            "source": source,
            "ext": ext,
            "width": width,
            "height": height,
            "orientation": orientation,
            "resolution_bucket": bucket_name,
            "source_site_id": source_site_id,
            "franchise": franchise,
            "purity": None,
            "enrichment_status": enrichment_status,
            "indexed_at": now,
            "metadata_path": str(metadata_path.resolve()) if metadata is not None else None,
            "source_url": source_url,
            "original_filename": original_filename,
            "canonical_filename": canonical_filename,
            "slug": slug,
            "sha256": sha256,
            "size_bytes": size_bytes,
            "transport": transport,
            "source_relative_path": source_relative_path,
            "download_recorded_at": download_recorded_at,
            "search_origins_json": search_origins_json,
        }
        image_id = _upsert_image(conn, values)
        _replace_image_tags(
            conn, image_id, source, tag_records,
            preserve_provenances=("wallhaven-api",) if source == "wallhaven" else (),
        )
        ledger_source_id = _normalise_wallhaven_source_id(source_site_id)
        if source == "wallhaven" and ledger_source_id in ledger_records:
            _apply_wallhaven_record(
                conn, image_id, ledger_records[ledger_source_id],
                refresh_derived=False,
            )
            applied_ledger_ids.add(ledger_source_id)
            stats["ledger_applied"] += 1
        if tag_records:
            stats["tagged"] += 1
        stats["anime_pictures" if source == "anime-pictures" else source] += 1

    # Reconcile only rows belonging to this scan root.  This is what makes a
    # post-orientation scan a true rebuild instead of accumulating stale paths.
    for row in conn.execute("SELECT id, path FROM images").fetchall():
        candidate = Path(row["path"])
        try:
            candidate.resolve().relative_to(library_root)
        except (OSError, ValueError):
            continue
        if os.path.normcase(str(candidate.resolve())) not in scanned_paths:
            conn.execute("DELETE FROM images WHERE id=?", (row["id"],))
            stats["stale_removed"] += 1
    conn.execute(
        "DELETE FROM tags WHERE NOT EXISTS "
        "(SELECT 1 FROM image_tags WHERE image_tags.tag_id=tags.id)"
    )
    provider_apply_stats = apply_provider_enrichment_records(
        conn, provider_records, refresh_derived=False,
    )
    for key in (
        "provider_records_matched", "provider_records_unmatched",
        "provider_images_updated", "provider_tags_applied",
    ):
        stats[key] = provider_apply_stats[key]
    refresh_derived_metadata(conn)
    stats["ledger_unmatched"] = len(
        set(ledger_records) - applied_ledger_ids
    )
    if stats["ledger_unmatched"]:
        logger.info(
            "%d Wallhaven ledger records did not match a canonical image",
            stats["ledger_unmatched"],
        )
    conn.commit()
    return stats


def _source_from_relative_path(path: Path) -> str:
    for part in reversed(path.parts[:-1]):
        source = canonical_source(part)
        if source in {"wallhaven", "zerochan", "anime-pictures", "unknown"}:
            return source
    return "unknown"


def _bucket_from_relative_path(path: Path) -> Optional[str]:
    buckets = {name.casefold(): name for name, _ in RESOLUTION_BUCKETS}
    buckets["_unknownresolution"] = "_UnknownResolution"
    for part in path.parts[:-1]:
        if part.casefold() in buckets:
            return buckets[part.casefold()]
    return None


def _upsert_image(conn: sqlite3.Connection, values: dict[str, object]) -> int:
    """Upsert by path, or by a moved file's stable source identity."""
    existing = conn.execute(
        "SELECT * FROM images WHERE path=?", (values["path"],),
    ).fetchone()
    if existing is None and os.name == "nt":
        existing = conn.execute(
            "SELECT * FROM images WHERE path=? COLLATE NOCASE ORDER BY id LIMIT 1",
            (values["path"],),
        ).fetchone()
    if existing is None and values["source"] != "unknown" and values["source_site_id"]:
        identity_rows = conn.execute(
            "SELECT * FROM images WHERE source=? AND source_site_id=? ORDER BY id",
            (values["source"], values["source_site_id"]),
        ).fetchall()
        # Reuse the identity only if its old physical path disappeared.  If it
        # still exists, this is a real duplicate and deserves its own path row.
        missing_identity_rows = [row for row in identity_rows if not Path(row["path"]).is_file()]
        if len(missing_identity_rows) == 1:
            existing = missing_identity_rows[0]

    columns = list(values)
    if existing is None:
        placeholders = ",".join("?" for _ in columns)
        row = conn.execute(
            f"INSERT INTO images ({','.join(columns)}) VALUES ({placeholders}) RETURNING id",
            tuple(values[column] for column in columns),
        ).fetchone()
        return int(row[0])

    # Preserve successful Wallhaven enrichment when a legacy (sidecar-free)
    # scan refreshes only physical/file fields.
    if (
        values["source"] == "wallhaven"
        and existing["enrichment_status"] in {STATUS_OK, STATUS_SKIPPED}
    ):
        values["enrichment_status"] = existing["enrichment_status"]
        values["franchise"] = existing["franchise"]
        values["purity"] = existing["purity"]
    assignments = ",".join(f"{column}=?" for column in columns)
    conn.execute(
        f"UPDATE images SET {assignments} WHERE id=?",
        tuple(values[column] for column in columns) + (existing["id"],),
    )
    return int(existing["id"])


def _replace_image_tags(
    conn: sqlite3.Connection,
    image_id: int,
    source: str,
    tags: Iterable[dict[str, str]],
    preserve_provenances: tuple[str, ...] = (),
) -> None:
    if preserve_provenances:
        placeholders = ",".join("?" for _ in preserve_provenances)
        conn.execute(
            "DELETE FROM image_tags WHERE image_id=? "
            f"AND COALESCE(provenance, '') NOT IN ({placeholders})",
            (image_id, *preserve_provenances),
        )
    else:
        conn.execute("DELETE FROM image_tags WHERE image_id = ?", (image_id,))
    for tag in tags:
        name = tag.get("name", "").strip()
        if not name:
            continue
        tag_type = tag.get("type", "unknown").strip() or "unknown"
        tag_id = _stable_tag_id(source, name, tag_type)
        conn.execute(
            "INSERT INTO tags(id, name, category_id, category, source, slug, "
            "tag_type, provenance) VALUES (?,?,NULL,NULL,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "source=excluded.source, slug=excluded.slug, "
            "tag_type=excluded.tag_type, provenance=excluded.provenance",
            (
                tag_id, name, canonical_source(source),
                tag.get("slug") or _tag_slug(name), tag_type,
                tag.get("provenance", "unknown") or "unknown",
            ),
        )
        provenance = tag.get("provenance", "unknown") or "unknown"
        conn.execute(
            "INSERT INTO image_tags(image_id, tag_id, provenance) VALUES (?,?,?) "
            "ON CONFLICT(image_id, tag_id) DO UPDATE SET "
            "provenance=excluded.provenance",
            (image_id, tag_id, provenance),
        )


def _replace_image_tags_by_name(
    conn: sqlite3.Connection, image_id: int, tag_names: Iterable[str],
    source: str = "anime-pictures",
) -> None:
    """Compatibility wrapper for legacy name-only tag callers."""
    _replace_image_tags(
        conn, image_id, source,
        (
            {
                "name": name,
                "slug": _tag_slug(name),
                "type": "unknown",
                "provenance": "legacy-filename",
            }
            for name in tag_names
        ),
    )


QUERY_SORTS = (
    "path",
    "newest",
    "oldest",
    "resolution_desc",
    "resolution_asc",
    "size_desc",
    "size_asc",
    "filename",
    "franchise",
    "source",
    "least_tagged",
    "rating_confidence",
    "shuffle",
)

_QUERY_ORDER_BY = {
    "path": (
        "images.path COLLATE NOCASE ASC, images.path ASC, images.id ASC"
    ),
    "newest": (
        "julianday(images.download_recorded_at) IS NULL ASC, "
        "julianday(images.download_recorded_at) DESC, images.id DESC"
    ),
    "oldest": (
        "julianday(images.download_recorded_at) IS NULL ASC, "
        "julianday(images.download_recorded_at) ASC, images.id ASC"
    ),
    "resolution_desc": (
        "(COALESCE(images.width, 0) * COALESCE(images.height, 0)) DESC, "
        "COALESCE(images.width, 0) DESC, COALESCE(images.height, 0) DESC, "
        "images.path COLLATE NOCASE ASC, images.path ASC, images.id ASC"
    ),
    "resolution_asc": (
        "CASE WHEN COALESCE(images.width, 0) > 0 "
        "AND COALESCE(images.height, 0) > 0 THEN 0 ELSE 1 END ASC, "
        "(COALESCE(images.width, 0) * COALESCE(images.height, 0)) ASC, "
        "COALESCE(images.width, 0) ASC, COALESCE(images.height, 0) ASC, "
        "images.path COLLATE NOCASE ASC, images.path ASC, images.id ASC"
    ),
    "size_desc": (
        "images.size_bytes IS NULL ASC, images.size_bytes DESC, "
        "images.path COLLATE NOCASE ASC, images.path ASC, images.id ASC"
    ),
    "size_asc": (
        "images.size_bytes IS NULL ASC, images.size_bytes ASC, "
        "images.path COLLATE NOCASE ASC, images.path ASC, images.id ASC"
    ),
    "filename": (
        "images.filename COLLATE NOCASE ASC, images.filename ASC, images.id ASC"
    ),
    "franchise": (
        "NULLIF(TRIM(images.franchise), '') IS NULL ASC, "
        "images.franchise COLLATE NOCASE ASC, images.franchise ASC, "
        "images.filename COLLATE NOCASE ASC, images.filename ASC, images.id ASC"
    ),
    "source": (
        "images.source COLLATE NOCASE ASC, images.source ASC, "
        "images.filename COLLATE NOCASE ASC, images.filename ASC, images.id ASC"
    ),
    "least_tagged": (
        "images.tag_count ASC, "
        "julianday(images.download_recorded_at) IS NULL ASC, "
        "julianday(images.download_recorded_at) DESC, images.id DESC"
    ),
    "rating_confidence": (
        "images.rating_confidence ASC, images.tag_count ASC, images.id ASC"
    ),
}


def query(
    conn: sqlite3.Connection,
    *,
    orientation: Optional[str] = None,
    franchise: Optional[str] = None,
    bucket: Optional[str] = None,
    source: Optional[str] = None,
    tag: Optional[str] = None,
    content_rating: Optional[str] = None,
    sort: str = "path",
    shuffle_seed: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ImageRow]:
    """Read-only query of the index. All filters are optional.

    Args:
        conn: Open DB connection.
        orientation: ``landscape`` | ``portrait`` | ``square``.
        franchise: Exact (case-insensitive) franchise match.
        bucket: Resolution bucket (``4K`` … ``SD``).
        source: Canonical source token (legacy ``anime_pictures`` is accepted).
        tag: Exact (case-insensitive) tag name.
        content_rating: ``sfw`` | ``suggestive`` | ``nsfw`` | ``unknown``.
        sort: Stable result order; one of :data:`QUERY_SORTS`.
        limit: Max rows to return.
        offset: Number of matching rows to skip for pagination.

    Returns:
        Matching :class:`ImageRow` instances.
    """
    sql = "SELECT images.* FROM images"
    where: list[str] = []
    params: list[object] = []
    if content_rating is not None:
        content_rating = content_rating.strip().casefold()
        if content_rating not in CONTENT_RATINGS:
            raise ValueError(
                "content_rating must be one of: " + ", ".join(CONTENT_RATINGS)
            )
        where.append("images.content_rating = ?")
        params.append(content_rating)
    if tag is not None:
        where.append(
            "EXISTS (SELECT 1 FROM image_tags it "
            "JOIN tags t ON t.id=it.tag_id WHERE it.image_id=images.id "
            "AND t.name=? COLLATE NOCASE)"
        )
        params.append(tag)
    if orientation is not None:
        where.append("images.orientation = ?")
        params.append(orientation)
    if franchise is not None:
        where.append("LOWER(images.franchise) = LOWER(?)")
        params.append(franchise)
    if bucket is not None:
        where.append("images.resolution_bucket = ?")
        params.append(bucket)
    if source is not None:
        where.append("images.source = ?")
        params.append(canonical_source(source))
    if where:
        sql += " WHERE " + " AND ".join(where)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be from 1 through 500")
    if offset < 0:
        raise ValueError("offset must be 0 or greater")
    sort = str(sort).strip().casefold()
    if sort not in QUERY_SORTS:
        raise ValueError("sort must be one of: " + ", ".join(QUERY_SORTS))
    if shuffle_seed is not None and (
        isinstance(shuffle_seed, bool)
        or not isinstance(shuffle_seed, int)
        or not 0 <= shuffle_seed <= 2_147_483_647
    ):
        raise ValueError("shuffle_seed must be from 0 through 2147483647")
    if sort == "shuffle":
        if shuffle_seed is None:
            raise ValueError("shuffle_seed is required when sort=shuffle")
        sql += " ORDER BY wallpaper_seed_rank(images.id, ?) ASC, images.id ASC"
        params.append(shuffle_seed)
    else:
        sql += " ORDER BY " + _QUERY_ORDER_BY[sort]
    sql += " LIMIT ? OFFSET ?"
    params.extend((limit, offset))
    rows = conn.execute(sql, params).fetchall()
    image_ids = tuple(int(row["id"]) for row in rows)
    tags_by_image: dict[int, list[IndexedTag]] = {
        image_id: [] for image_id in image_ids
    }
    if image_ids:
        placeholders = ",".join("?" for _ in image_ids)
        tag_rows = conn.execute(
            "SELECT it.image_id, t.name, t.slug, t.tag_type, t.provenance, "
            "t.source, t.category, it.provenance AS association_provenance "
            "FROM tags t JOIN image_tags it ON it.tag_id=t.id "
            f"WHERE it.image_id IN ({placeholders}) "
            "ORDER BY it.image_id, t.name COLLATE NOCASE, t.name, "
            "COALESCE(t.tag_type, t.category, 'unknown') COLLATE NOCASE, "
            "COALESCE(t.tag_type, t.category, 'unknown'), "
            "COALESCE(t.source, '') COLLATE NOCASE, "
            "COALESCE(it.provenance, t.provenance, '') COLLATE NOCASE, t.id",
            image_ids,
        ).fetchall()
        source_by_image = {int(row["id"]): str(row["source"]) for row in rows}
        for tag_row in tag_rows:
            image_id = int(tag_row["image_id"])
            tags_by_image[image_id].append(
                IndexedTag(
                    name=str(tag_row["name"]),
                    slug=str(tag_row["slug"] or _tag_slug(tag_row["name"])),
                    type=str(
                        tag_row["tag_type"] or tag_row["category"] or "unknown"
                    ),
                    provenance=str(
                        tag_row["association_provenance"]
                        or tag_row["provenance"] or "legacy-index"
                    ),
                    source=str(
                        tag_row["source"] or source_by_image[image_id]
                    ),
                )
            )
    suggestions = list_tag_suggestions(conn, image_ids)
    return [
        ImageRow.from_row(
            row, tags_by_image[int(row["id"])], suggestions[int(row["id"])],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Orientation reorg plan (Phase 3 — read-only manifest emit)
# ---------------------------------------------------------------------------

def destination_for(row: sqlite3.Row) -> tuple[str, str]:
    """Compute the orientation-reorg destination for one image row.

    Pure function: returns a ``(relative_directory, orientation)`` pair. The
    directory is relative to the library root and always uses the layout
    ``<bucket>/<orientation>/<source>`` with canonical source tokens. This
    module never moves the file —
    ``sort-by-orientation.ps1`` consumes the manifest this informs.

    Args:
        row: An ``images`` row with at least ``source``, ``resolution_bucket``,
            and ``orientation`` columns.

    Returns:
        ``(relative_destination_dir, orientation)``. The orientation is
        ``"unknown"`` for rows with no readable dimensions.
    """
    source = canonical_source(row["source"] or "unknown")
    if source not in {"wallhaven", "zerochan", "anime-pictures", "unknown"}:
        source = "unknown"
    bucket = row["resolution_bucket"] or "_UnknownResolution"
    orientation = row["orientation"] or UNKNOWN_ORIENTATION
    return f"{bucket}/{orientation}/{source}", orientation



# ---------------------------------------------------------------------------
# HTML activity dashboard (read-only: file mtimes + index -> one HTML file)
# ---------------------------------------------------------------------------

def generate_dashboard(
    conn, out_path, library_root,
):
    """Scan image mtimes + read enrichment data; emit a self-contained HTML dashboard.

    Read-only on the library: stats files, queries the DB, and writes exactly
    one HTML file. Never moves, deletes, or renames images.

    Args:
        conn: Open DB connection to ``wallpaper_index.sqlite``
            (``row_factory`` should be ``sqlite3.Row``).
        out_path: Destination HTML path (parent dir is created if needed).
        library_root: The ``images`` directory (label only, not scanned).

    Returns:
        Stats dict: ``{"emitted": 1, "present": n, "missing": n, "days": n}``.
    """
    import html as _html
    import time as _time
    from collections import Counter as _Counter

    agg = _collect_activity(conn)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html_text = _render_html(agg, library_root)
    out_path.write_text(html_text, encoding="utf-8")
    return {
        "emitted": 1,
        "present": agg["present"],
        "missing": agg["missing"],
        "days": len(agg["per_day"]),
    }


def _collect_activity(conn):
    """One pass over all image rows: stat mtime, accumulate all aggregates."""
    import time as _time
    from collections import Counter as _Counter

    per_day = _Counter()
    per_hour = _Counter()
    by_source = _Counter()
    by_bucket = _Counter()
    by_orientation = _Counter()
    by_purity = _Counter()
    by_franchise = _Counter()
    by_ext = _Counter()
    present = 0
    missing = 0
    earliest = None
    latest = None

    rows = conn.execute(
        "SELECT path, source, franchise, purity, resolution_bucket, "
        "orientation, ext FROM images"
    ).fetchall()

    for row in rows:
        path_str = row["path"]
        source = row["source"] or "unknown"
        bucket = row["resolution_bucket"] or "unknown"
        orientation = row["orientation"] or "unknown"
        purity = row["purity"] or "unknown"
        franchise = row["franchise"]
        ext = (row["ext"] or "unknown").lower()

        try:
            st = Path(path_str).stat()
        except OSError:
            missing += 1
            continue
        present += 1

        mtime = st.st_mtime
        if earliest is None or mtime < earliest:
            earliest = mtime
        if latest is None or mtime > latest:
            latest = mtime

        lt = _time.localtime(mtime)
        day = _time.strftime("%Y-%m-%d", lt)
        per_day[day] += 1
        per_hour[lt.tm_hour] += 1

        by_source[source] += 1
        by_bucket[bucket] += 1
        by_orientation[orientation] += 1
        by_purity[purity] += 1
        by_ext[ext] += 1
        if franchise:
            by_franchise[franchise] += 1

    return {
        "total_rows": len(rows),
        "present": present,
        "missing": missing,
        "earliest": earliest,
        "latest": latest,
        "per_day": per_day,
        "per_hour": per_hour,
        "by_source": by_source,
        "by_bucket": by_bucket,
        "by_orientation": by_orientation,
        "by_purity": by_purity,
        "by_franchise": by_franchise,
        "by_ext": by_ext,
    }


_DASHBOARD_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
html { background: #070b12; scroll-behavior: smooth; }
body { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  margin: 0 auto; padding: 28px; width: 100%; max-width: 1320px;
  background: radial-gradient(circle at top left, #152038 0, #0d1117 38%, #080c13 100%);
  color: #c9d1d9; line-height: 1.5; min-height: 100vh; }
h1 { color: #f0f6fc; border-bottom: 1px solid #30363d; padding-bottom: 8px; }
h2 { color: #f0f6fc; margin-top: 40px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
h3 { color: #79c0ff; margin-top: 28px; }
.lede { color: #9da7b3; max-width: 72ch; margin-top: -4px; }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px 16px; }
.card .label { color: #8b949e; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }
.card .value { color: #f0f6fc; font-size: 26px; font-weight: 600; }
nav { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px 16px; margin: 16px 0;
  display: flex; flex-wrap: wrap; gap: 6px 18px; }
nav a { color: #58a6ff; text-decoration: none; font-size: 14px; }
nav a:hover { text-decoration: underline; }
.chart-wrap { overflow-x: auto; background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 12px; margin: 12px 0; }
.chart-wrap svg { display: block; width: 100%; height: auto; min-width: 620px; }
svg .bar { fill: #58a6ff; }
svg .bar:hover { fill: #79c0ff; }
svg .axis { fill: #8b949e; font-size: 11px; }
svg .grid { stroke: #21262d; stroke-width: 1; }
.bars-vert rect { transition: opacity 0.15s; }
.bars-vert rect:hover { opacity: 0.8; }
.panel-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; align-items: start; }
.panel { min-width: 0; background: #111720; border: 1px solid #27303b; border-radius: 10px; padding: 4px 16px 12px; }
.panel h3 { margin-top: 14px; }
table.brk { border-collapse: collapse; width: 100%; margin: 12px 0; }
table.brk th { color: #8b949e; font-size: 11px; font-weight: 600; text-align: left; text-transform: uppercase; letter-spacing: .45px;
  padding: 4px 8px; border-bottom: 1px solid #30363d; }
table.brk th.count { text-align: right; }
table.brk td { padding: 4px 8px; border-bottom: 1px solid #21262d; }
table.brk td.name { color: #c9d1d9; }
table.brk td.count { color: #8b949e; text-align: right; white-space: nowrap; }
.bar-cell { background: #58a6ff; height: 14px; border-radius: 2px; min-width: 2px; display: inline-block; vertical-align: middle; }
.footer { color: #484f58; font-size: 12px; margin-top: 40px; border-top: 1px solid #21262d; padding-top: 12px; }
@media (max-width: 760px) {
  body { padding: 18px 14px 44px; }
  h1 { font-size: 26px; }
  .summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .card .value { font-size: 22px; }
  .panel-grid { grid-template-columns: 1fr; }
  .chart-wrap { padding: 8px; }
}
@media (max-width: 430px) {
  .summary { grid-template-columns: 1fr; }
}
""".strip()


def _fmt_ts(ts):
    import time as _time
    if ts is None:
        return "\u2014"
    return _time.strftime("%Y-%m-%d", _time.localtime(ts))


def _esc(s):
    import html as _html
    return _html.escape(str(s))


def _render_svg_timeline(per_day, window_days=365):
    """Render recent daily activity at a bounded, responsive width."""
    from datetime import date, timedelta

    if not per_day:
        return '<p class="axis">No activity data.</p>'
    days_sorted = sorted(per_day)
    all_first = date.fromisoformat(days_sorted[0])
    last = date.fromisoformat(days_sorted[-1])
    first = max(all_first, last - timedelta(days=max(1, window_days) - 1))
    span = []
    d = first
    while d <= last:
        iso = d.isoformat()
        span.append((iso, per_day.get(iso, 0)))
        d += timedelta(days=1)

    max_count = max(c for _, c in span) or 1
    n = len(span)
    width = 960
    height = 250
    left, right, top, bottom = 46, 14, 26, 34
    chart_h = height - top - bottom
    step = (width - left - right) / max(1, n)
    bar_w = max(1.0, step * 0.72)
    bars = []
    for i, (iso, count) in enumerate(span):
        if count == 0:
            continue
        x = left + i * step + (step - bar_w) / 2
        bar_h = max(1.0, (count / max_count) * chart_h)
        y = top + chart_h - bar_h
        bars.append(
            '<rect class="bar" x="%.1f" y="%.1f" width="%.2f" height="%.1f"><title>%s: %d image(s)</title></rect>'
            % (x, y, bar_w, bar_h, _esc(iso), count)
        )

    peak_iso, peak_count = max(per_day.items(), key=lambda kv: kv[1])
    recent_peak_iso, recent_peak_count = max(span, key=lambda kv: kv[1])
    labels = [
        '<text class="axis" x="%d" y="16">Recent peak: %s (%d)</text>'
        % (left, _esc(recent_peak_iso), recent_peak_count),
        '<text class="axis" x="%d" y="16" text-anchor="end">All-time peak: %s (%d)</text>'
        % (width - right, _esc(peak_iso), peak_count),
    ]
    grid = []
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = top + chart_h - frac * chart_h
        grid.append(
            '<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
            % (left, gy, width - right, gy)
        )
        grid.append(
            '<text class="axis" x="%d" y="%.1f" text-anchor="end">%d</text>'
            % (left - 6, gy + 4, int(max_count * frac))
        )
    month_marks = []
    for i, (iso, _count) in enumerate(span):
        current = date.fromisoformat(iso)
        if current.day != 1 and i != 0:
            continue
        x = left + i * step
        month_marks.append(
            '<line class="grid" x1="%.1f" y1="%d" x2="%.1f" y2="%d"/>'
            % (x, top, x, top + chart_h)
        )
        month_marks.append(
            '<text class="axis" x="%.1f" y="%d">%s</text>'
            % (x + 3, height - 10, _esc(current.strftime("%b %Y")))
        )
    aria = "Daily wallpaper activity from %s through %s" % (
        first.isoformat(), last.isoformat(),
    )
    return (
        '<svg class="bars-vert" width="100%%" viewBox="0 0 %d %d" role="img" '
        'aria-label="%s" preserveAspectRatio="xMidYMid meet">'
        % (width, height, _esc(aria))
        + "".join(grid)
        + "".join(month_marks)
        + "".join(bars)
        + "".join(labels)
        + "</svg>"
    )


def _render_svg_hourly(per_hour):
    width = 760
    height = 200
    chart_h = height - 40
    left = 40
    max_c = max((per_hour.get(h, 0) for h in range(24)), default=1) or 1
    bar_w = (width - left - 20) / 24
    bars = []
    for h in range(24):
        c = per_hour.get(h, 0)
        x = left + h * bar_w
        bh = (c / max_c) * chart_h if c else 0
        y = chart_h - bh + 10
        bars.append(
            '<rect class="bar" x="%.1f" y="%.1f" width="%.1f" height="%.1f"><title>%02d:00 \u2014 %d image(s)</title></rect>'
            % (x, y, bar_w - 2, bh, h, c)
        )
        bars.append(
            '<text class="axis" x="%.1f" y="%d">%d</text>' % (x + bar_w / 2, height - 22, h)
        )
    grid = []
    for frac in (0.5, 1.0):
        gy = chart_h - frac * chart_h + 10
        grid.append('<line class="grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>' % (left, gy, width - 10, gy))
        grid.append('<text class="axis" x="4" y="%.0f">%d</text>' % (gy + 4, int(frac * max_c)))
    return (
        '<svg width="100%%" viewBox="0 0 %d %d" role="img" '
        'aria-label="Wallpaper saves by local hour" preserveAspectRatio="xMidYMid meet">'
        % (width, height)
        + "".join(grid)
        + "".join(bars)
        + "</svg>"
    )


def _render_breakdown(title, counter, top=0):
    items = counter.most_common(top if top else None)
    if not items:
        return "<h3>%s</h3><p class='axis'>No data.</p>" % _esc(title)
    total = sum(counter.values()) or 1
    max_c = items[0][1] or 1
    rows = []
    for name, count in items:
        pct = count / total * 100
        bar_w = count / max_c * 100
        rows.append(
            "<tr><td class=\"name\">%s</td>"
            "<td style=\"width:45%%\"><span class=\"bar-cell\" style=\"width:%.1f%%\"></span></td>"
            "<td class=\"count\">%d (%.1f%%)</td></tr>"
            % (_esc(name), bar_w, count, pct)
        )
    return (
        "<h3>%s</h3><table class=\"brk\">"
        "<thead><tr><th scope=\"col\">Category</th>"
        "<th scope=\"col\">Share</th>"
        "<th scope=\"col\" class=\"count\">Count</th></tr></thead>"
        "<tbody>%s</tbody></table>"
    ) % (_esc(title), "".join(rows))


def _render_html(agg, library_root):
    from datetime import date

    generated = _now_iso_local()
    span_str = (
        "%s \u2192 %s" % (_fmt_ts(agg["earliest"]), _fmt_ts(agg["latest"]))
        if agg["earliest"]
        else "\u2014"
    )
    days_with_activity = len(agg["per_day"])
    peak_iso, peak_count = (
        max(agg["per_day"].items(), key=lambda kv: kv[1])
        if agg["per_day"] else ("—", 0)
    )
    if agg["per_day"]:
        first_day = date.fromisoformat(min(agg["per_day"]))
        last_day = date.fromisoformat(max(agg["per_day"]))
        recent_span_days = min(365, (last_day - first_day).days + 1)
    else:
        recent_span_days = 0

    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wallpaper Activity Dashboard</title>
<style>%s</style>
</head>
<body>
<h1>\U0001F5BC\uFE0F Wallpaper Activity Dashboard</h1>
<p class="lede">A long-term view of the canonical library: when files arrived, where they came from,
and how the collection is distributed. Queue operations live in the separate download dashboard.</p>

<div class="summary">
  <div class="card"><div class="label">Total images</div><div class="value">%s</div></div>
  <div class="card"><div class="label">On disk</div><div class="value">%s</div></div>
  <div class="card"><div class="label">Missing</div><div class="value">%s</div></div>
  <div class="card"><div class="label">Distinct days</div><div class="value">%s</div></div>
  <div class="card"><div class="label">Date span</div><div class="value" style="font-size:16px">%s</div></div>
  <div class="card"><div class="label">Busiest day</div><div class="value" style="font-size:16px">%s</div><div class="label">%s images</div></div>
</div>

<nav aria-label="Dashboard sections">
  <a href="#timeline">Recent activity</a>
  <a href="#hourly">Hour of day</a>
  <a href="#source">Source</a>
  <a href="#resolution">Resolution</a>
  <a href="#orientation">Orientation</a>
  <a href="#purity">Purity</a>
  <a href="#franchise">Franchises</a>
  <a href="#format">Format</a>
</nav>

<h2 id="timeline">\U0001F4C5 Recent daily activity</h2>
<p class="axis">The latest %s-day calendar window by file mtime, scaled independently from the all-time peak.
Hover a bar for its date and count. The full history contains %s days with activity.</p>
<div class="chart-wrap">
%s
</div>

<h2 id="hourly">\u23F0 Hour of day</h2>
<p class="axis">When images were saved (local time), by hour 0\u201323.</p>
<div class="chart-wrap">
%s
</div>

<h2 id="breakdowns">Library breakdowns</h2>
<div class="panel-grid">
  <section class="panel" id="source">%s</section>
  <section class="panel" id="resolution">%s</section>
  <section class="panel" id="orientation">%s</section>
  <section class="panel" id="purity">%s</section>
  <section class="panel" id="franchise">%s</section>
  <section class="panel" id="format">%s</section>
</div>

<div class="footer">
  Generated %s from <code>%s</code>.<br>
  Read-only: built from the SQLite index + file mtimes; no images were moved.
</div>
</body>
</html>
""" % (
        _DASHBOARD_CSS,
        format(agg["total_rows"], ","),
        format(agg["present"], ","),
        format(agg["missing"], ","),
        format(days_with_activity, ","),
        _esc(span_str),
        _esc(peak_iso),
        format(peak_count, ","),
        format(recent_span_days, ","),
        format(days_with_activity, ","),
        _render_svg_timeline(agg["per_day"]),
        _render_svg_hourly(agg["per_hour"]),
        _render_breakdown("Source", agg["by_source"]),
        _render_breakdown("Resolution bucket", agg["by_bucket"]),
        _render_breakdown("Orientation", agg["by_orientation"]),
        _render_breakdown("Purity (enriched)", agg["by_purity"]),
        _render_breakdown("Top 20 franchises", agg["by_franchise"], top=20),
        _render_breakdown("Extension", agg["by_ext"]),
        _esc(generated),
        _esc(library_root),
    )


def _now_iso_local():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit_reorg_plan(
    conn: sqlite3.Connection, plan_path: Path, library_root: Path,
) -> dict[str, int]:
    """Write a CSV manifest of every indexed image's orientation destination.

    This is the read-only handoff from the Python index to the PowerShell
    reorg. For each ``images`` row it emits one line with the source path
    (absolute, as indexed) and the relative destination directory computed by
    :func:`destination_for`. ``sort-by-orientation.ps1`` reads this CSV and
    performs the same-volume ``[IO.File]::Move`` calls — the actual file
    mutation stays in PowerShell, per ``project-conventions.md``.

    Rows whose source file no longer exists (e.g. already moved by a prior
    apply, or deleted out of band) are skipped and counted, so re-running the
    plan after a partial apply is safe.

    Args:
        conn: Open DB connection.
        plan_path: Where to write the CSV (parent is created if needed).
        library_root: The ``images`` directory — used only to sanity-check that
            indexed paths live under it.

    Returns:
        Counts for rows emitted, missing, and rejected as outside the root.
    """
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    library_root = library_root.resolve()
    stats = {"emitted": 0, "missing": 0, "outside_root": 0}
    rows = conn.execute(
        "SELECT path, source, resolution_bucket, orientation FROM images "
        "ORDER BY path"
    ).fetchall()
    with plan_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["SourcePath", "DestinationDir", "Orientation", "Source"])
        for row in rows:
            p = row["path"]
            try:
                Path(p).resolve().relative_to(library_root)
            except (OSError, ValueError):
                stats["outside_root"] += 1
                continue
            if not Path(p).is_file():
                stats["missing"] += 1
                continue
            dest_dir, orientation = destination_for(row)
            writer.writerow([p, dest_dir, orientation, row["source"]])
            stats["emitted"] += 1
    return stats


# ---------------------------------------------------------------------------
# Wallhaven enrichment (Phase 2 — network)
# ---------------------------------------------------------------------------

def load_api_key(env_path: Path) -> Optional[str]:
    """Read ``WALLHAVEN_API_KEY`` from a ``KEY=VALUE`` ``.env`` file.

    Does not depend on python-dotenv (not a declared runtime dep). Comments
    and blank lines are skipped; inline comments are not stripped. Returns
    ``None`` if the key is absent or the file can't be read.

    Args:
        env_path: Path to the ``.env`` file.

    Returns:
        The key value, or ``None``.
    """
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "WALLHAVEN_API_KEY":
            value = value.strip().strip('"').strip("'")
            return value or None
    return None


def _wallhaven_get(
    url: str, *, retries: int = 3, backoff: float = 2.0,
) -> bytes:
    """GET with retry/backoff on transient errors, mirroring
    ``anime_pictures._http_get`` (lines 339-390).

    Returns the response body on success. Raises ``HTTPError`` for non-
    transient codes (e.g. 401, 404) so the caller can mark the row skipped.
    """
    last_exc: Optional[Exception] = None
    delay = max(backoff, 0.0)
    for attempt in range(1, retries + 2):
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_REQUEST_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code in (429, 500, 502, 503, 504):
                logger.debug(
                    "GET %s -> HTTP %d (attempt %d/%d), sleeping %.1fs",
                    url, exc.code, attempt, retries + 1, delay,
                )
            else:
                raise  # 401/403/404 etc. — caller decides
        except urllib.error.URLError as exc:
            last_exc = exc
            logger.debug(
                "GET %s -> URLError %s (attempt %d/%d), sleeping %.1fs",
                url, exc, attempt, retries + 1, delay,
            )
        if attempt > retries:
            break
        time.sleep(min(delay, MAX_BACKOFF_SECONDS))
        delay *= 2.0
    assert last_exc is not None
    raise last_exc


def _wallhaven_record_from_api(
    source_id: str, data: dict,
) -> dict[str, object]:
    tags: list[dict[str, object]] = []
    franchise = None
    for raw_tag in data.get("tags") or []:
        if not isinstance(raw_tag, dict):
            continue
        tag_name = _optional_text(raw_tag.get("name"))
        if tag_name is None:
            continue
        category = _optional_text(raw_tag.get("category"))
        if franchise is None and category in FRANCHISE_CATEGORIES:
            franchise = tag_name
        tags.append(
            _normalise_ledger_tag(
                {
                    "name": tag_name,
                    "slug": raw_tag.get("slug") or _tag_slug(tag_name),
                    "type": category or "unknown",
                    "category_id": raw_tag.get("category_id"),
                    "category": category,
                    "provenance": WALLHAVEN_LEDGER_PROVENANCE,
                }
            )
        )
    return _normalise_wallhaven_ledger_record(
        {
            "schema_version": WALLHAVEN_LEDGER_SCHEMA_VERSION,
            "record_type": WALLHAVEN_LEDGER_RECORD_TYPE,
            "source": "wallhaven",
            "source_id": source_id,
            "enrichment_status": STATUS_OK,
            "franchise": franchise,
            "purity": data.get("purity"),
            "provenance": WALLHAVEN_LEDGER_PROVENANCE,
            "tags": tags,
            "captured_at": _now_iso(),
            "error": None,
        }
    )


def _wallhaven_status_record(
    source_id: str, status: str, *, error: Optional[str] = None,
) -> dict[str, object]:
    return _normalise_wallhaven_ledger_record(
        {
            "schema_version": WALLHAVEN_LEDGER_SCHEMA_VERSION,
            "record_type": WALLHAVEN_LEDGER_RECORD_TYPE,
            "source": "wallhaven",
            "source_id": source_id,
            "enrichment_status": status,
            "franchise": None,
            "purity": None,
            "provenance": WALLHAVEN_LEDGER_PROVENANCE,
            "tags": [],
            "captured_at": _now_iso(),
            "error": error,
        }
    )


def _apply_wallhaven_record(
    conn: sqlite3.Connection, image_id: int, record: dict[str, object],
    *, refresh_derived: bool = True,
) -> None:
    """Apply one durable Wallhaven record without replacing sidecar tags."""
    record = _normalise_wallhaven_ledger_record(record)
    if record["enrichment_status"] == STATUS_PENDING:
        conn.execute(
            "UPDATE images SET enrichment_status=? WHERE id=?",
            (STATUS_PENDING, image_id),
        )
        if refresh_derived:
            refresh_derived_metadata(conn, (image_id,))
        return
    conn.execute(
        "DELETE FROM image_tags WHERE image_id=? AND provenance='wallhaven-api'",
        (image_id,),
    )
    for tag in record["tags"]:
        assert isinstance(tag, dict)
        tag_name = str(tag["name"])
        tag_type = str(tag["type"])
        tag_id = _stable_tag_id("wallhaven", tag_name, tag_type)
        conn.execute(
            "INSERT INTO tags(id, name, category_id, category, source, slug, "
            "tag_type, provenance) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "category_id=excluded.category_id, category=excluded.category, "
            "source=excluded.source, slug=excluded.slug, "
            "tag_type=excluded.tag_type, "
            "provenance=COALESCE(tags.provenance,excluded.provenance)",
            (
                tag_id, tag_name, tag.get("category_id"), tag.get("category"),
                "wallhaven", tag.get("slug") or _tag_slug(tag_name), tag_type,
                WALLHAVEN_LEDGER_PROVENANCE,
            ),
        )
        conn.execute(
            "INSERT INTO image_tags(image_id, tag_id, provenance) "
            "VALUES (?,?,'wallhaven-api') "
            "ON CONFLICT(image_id, tag_id) DO NOTHING",
            (image_id, tag_id),
        )
    if record["enrichment_status"] == STATUS_OK:
        conn.execute(
            "UPDATE images SET franchise=?, purity=?, enrichment_status=? "
            "WHERE id=?",
            (
                record["franchise"], record["purity"],
                record["enrichment_status"], image_id,
            ),
        )
    else:
        # A status-only result (notably HTTP 401 -> skipped) carries no API
        # classification and must not erase a sidecar-derived franchise.
        conn.execute(
            "UPDATE images SET franchise=COALESCE(?, franchise), "
            "purity=COALESCE(?, purity), enrichment_status=? WHERE id=?",
            (
                record["franchise"], record["purity"],
                record["enrichment_status"], image_id,
            ),
        )
    conn.execute(
        "DELETE FROM tags WHERE NOT EXISTS "
        "(SELECT 1 FROM image_tags WHERE image_tags.tag_id=tags.id)"
    )
    if refresh_derived:
        refresh_derived_metadata(conn, (image_id,))


def enrich_wallhaven(
    conn: sqlite3.Connection,
    api_key: Optional[str],
    *,
    max_fetch: Optional[int] = None,
    max_attempts: Optional[int] = None,
    sleep_seconds: float = WALLHAVEN_SLEEP_SECONDS,
    ledger_path: Optional[Path] = None,
) -> dict[str, int]:
    """Fetch Wallhaven metadata for pending rows. Resumable.

    Pending status is the complete work queue; the progress cursor is only
    telemetry and never excludes a row. An attempt record and then its terminal
    result are fsynced before SQLite status/progress changes.

    Args:
        conn: Open DB connection.
        api_key: Optional Wallhaven API key. Without it, NSFW rows return 401
            and are marked ``skipped``.
        max_fetch: Stop after this many successes. It also bounds attempts when
            ``max_attempts`` is omitted, making one-item canaries truly bounded.
        max_attempts: Stop after this many attempted rows (``None`` = all).
        sleep_seconds: Politeness sleep between calls.

    Returns:
        Stats include attempted, fetched, skipped, failed, and remaining.
    """
    if ledger_path is None:
        raise ValueError("ledger_path is required for durable enrichment")
    ledger_path = Path(ledger_path)
    for name, value in (("max_fetch", max_fetch), ("max_attempts", max_attempts)):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 1
        ):
            raise ValueError(f"{name} must be a positive integer or null")
    if sleep_seconds < 0:
        raise ValueError("sleep_seconds must be 0 or greater")
    effective_attempt_limit = max_attempts
    if effective_attempt_limit is None and max_fetch is not None:
        effective_attempt_limit = max_fetch

    rows = conn.execute(
        "SELECT id, source_site_id FROM images "
        "WHERE source = 'wallhaven' AND enrichment_status = ? "
        "AND source_site_id IS NOT NULL "
        "ORDER BY LOWER(CAST(source_site_id AS TEXT)), "
        "CAST(source_site_id AS TEXT) COLLATE BINARY, id",
        (STATUS_PENDING,),
    ).fetchall()
    stats = {
        "attempted": 0, "fetched": 0, "skipped": 0,
        "failed": 0, "remaining": len(rows),
    }
    affected_ids: set[int] = set()

    def publish_batch() -> None:
        if not affected_ids:
            return
        refresh_derived_metadata(conn, affected_ids)
        conn.commit()
        affected_ids.clear()

    try:
        for row in rows:
            if max_fetch is not None and stats["fetched"] >= max_fetch:
                break
            if (
                effective_attempt_limit is not None
                and stats["attempted"] >= effective_attempt_limit
            ):
                break
            image_id = int(row["id"])
            site_id = str(row["source_site_id"])
            _append_wallhaven_ledger_record(
                ledger_path,
                _wallhaven_status_record(site_id, STATUS_PENDING),
            )
            stats["attempted"] += 1
            url = WALLHAVEN_DETAIL_URL.format(id=site_id)
            if api_key:
                url += f"?apikey={urllib.parse.quote(api_key)}"
            data: dict[str, object] = {}
            try:
                body = _wallhaven_get(url)
                payload = json.loads(body.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Wallhaven response must be an object")
                raw_data = payload.get("data") or {}
                if not isinstance(raw_data, dict):
                    raise ValueError("Wallhaven data must be an object")
                data = raw_data
                record = _wallhaven_record_from_api(site_id, data)
                _append_wallhaven_ledger_record(ledger_path, record)
                _apply_wallhaven_data(
                    conn, image_id, data, record=record,
                    refresh_derived=False,
                )
                stats["fetched"] += 1
            except urllib.error.HTTPError as exc:
                status = STATUS_SKIPPED if exc.code == 401 else STATUS_FAILED
                record = _wallhaven_status_record(
                    site_id, status, error=f"HTTP {exc.code}",
                )
                _append_wallhaven_ledger_record(ledger_path, record)
                _apply_wallhaven_record(
                    conn, image_id, record, refresh_derived=False,
                )
                if status == STATUS_SKIPPED:
                    stats["skipped"] += 1
                else:
                    logger.warning(
                        "wallhaven %s -> HTTP %d; durable failure recorded",
                        site_id, exc.code,
                    )
                    stats["failed"] += 1
            except (urllib.error.URLError, json.JSONDecodeError, ValueError) as exc:
                logger.warning("wallhaven %s -> %s; durable failure recorded", site_id, exc)
                record = _wallhaven_status_record(
                    site_id, STATUS_FAILED, error=f"{type(exc).__name__}: {exc}",
                )
                _append_wallhaven_ledger_record(ledger_path, record)
                _apply_wallhaven_record(
                    conn, image_id, record, refresh_derived=False,
                )
                stats["failed"] += 1

            affected_ids.add(image_id)
            conn.execute(
                "INSERT INTO enrichment_progress("
                "source,last_processed_source_site_id,updated_at) "
                "VALUES ('wallhaven', ?, ?) ON CONFLICT(source) DO UPDATE SET "
                "last_processed_source_site_id=excluded.last_processed_source_site_id, "
                "updated_at=excluded.updated_at",
                (site_id, _now_iso()),
            )
            if len(affected_ids) >= 50:
                publish_batch()
                logger.info(
                    "wallhaven enrichment: %d attempted, %d fetched, "
                    "%d skipped, %d failed",
                    stats["attempted"], stats["fetched"],
                    stats["skipped"], stats["failed"],
                )
            if (
                (effective_attempt_limit is None
                 or stats["attempted"] < effective_attempt_limit)
                and (max_fetch is None or stats["fetched"] < max_fetch)
                and stats["attempted"] < len(rows)
            ):
                time.sleep(sleep_seconds)
        publish_batch()
    except BaseException:
        conn.rollback()
        raise

    stats["remaining"] = conn.execute(
        "SELECT COUNT(*) AS c FROM images WHERE source='wallhaven' "
        "AND enrichment_status = ?", (STATUS_PENDING,),
    ).fetchone()["c"]
    return stats


def _apply_wallhaven_data(
    conn: sqlite3.Connection, image_id: int, data: dict,
    *, record: Optional[dict[str, object]] = None,
    refresh_derived: bool = True,
) -> None:
    """Write one Wallhaven ``data`` object to the image row + tags tables."""
    width = data.get("dimension_x")
    height = data.get("dimension_y")
    orientation = derive_orientation(width, height)
    if record is None:
        row = conn.execute(
            "SELECT source_site_id FROM images WHERE id=?", (image_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"image id does not exist: {image_id}")
        record = _wallhaven_record_from_api(row["source_site_id"], data)
    _apply_wallhaven_record(conn, image_id, record, refresh_derived=False)

    # Prefer API dimensions over filename-derived ones only when the API gave
    # real numbers; some rows return null dims. Franchise, purity, status, and
    # typed tags were applied from the durable record above.
    if width and height:
        conn.execute(
            "UPDATE images SET width=?, height=?, orientation=? WHERE id=?",
            (width, height, orientation, image_id),
        )
    if refresh_derived:
        refresh_derived_metadata(conn, (image_id,))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_query_arg(raw: str) -> dict[str, str]:
    """Parse ``key=value`` pairs from repeated ``--query`` tokens."""
    out: dict[str, str] = {}
    for token in shlex.split(raw):
        if "=" not in token:
            continue
        k, _, v = token.partition("=")
        out[k.strip()] = v.strip()
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Run with ``python -m dl_engine.index_library``."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="dl_engine.index_library",
        description="Rebuildable SQLite index of the wallpaper library.",
    )
    parser.add_argument(
        "--library-root", type=Path, default=None,
        help="Explicit canonical library root.",
    )
    parser.add_argument(
        "--db-path", type=Path, default=None,
        help="Explicit SQLite index path.",
    )
    parser.add_argument(
        "--ledger-path", type=Path, default=None,
        help="Explicit durable Wallhaven enrichment ledger path.",
    )
    parser.add_argument(
        "--provider-ledger-path", type=Path, default=None,
        help="Explicit durable generic provider-enrichment ledger path.",
    )
    parser.add_argument(
        "--apply-provider-ledger", action="store_true",
        help="Offline-apply --provider-ledger-path to the schema-3 index.",
    )
    parser.add_argument(
        "--export-wallhaven-ledger", type=Path, default=None, metavar="PATH",
        help="Read --db-path in SQLite mode=ro and atomically export its "
             "Wallhaven API metadata to PATH, without opening/migrating the "
             "normal index.",
    )
    parser.add_argument(
        "--enrich", action="store_true",
        help="Run the Wallhaven API enrichment pass (network; resumable).",
    )
    parser.add_argument(
        "--max-fetch", type=int, default=None,
        help="Stop enrichment after this many successes (also bounds attempts "
             "unless --max-attempts is given).",
    )
    parser.add_argument(
        "--max-attempts", type=int, default=None,
        help="Stop enrichment after this many attempted pending rows.",
    )
    parser.add_argument(
        "--env-path", type=Path, default=None,
        help="Explicit .env path for WALLHAVEN_API_KEY.",
    )
    parser.add_argument(
        "--query", metavar="KEY=VALUE ...", default=None,
        help="Read-only query, e.g. '--query orientation=portrait source=wallhaven'.",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print index stats and exit.",
    )
    parser.add_argument(
        "--emit-plan", metavar="CSV_PATH", default=None,
        help="Write a CSV manifest of each image's orientation destination for "
             "sort-by-orientation.ps1 to consume. Read-only on the library.",
    )
    parser.add_argument(
        "--dashboard", metavar="HTML_PATH", default=None,
        help="Emit a self-contained HTML activity dashboard (per-day timeline, "
             "hour-of-day distribution, source/resolution/purity/franchise "
             "breakdowns) built from file mtimes + the index, and exit. "
             "Read-only on the library.",
    )
    verification_group = parser.add_mutually_exclusive_group()
    verification_group.add_argument(
        "--verify", action="store_true",
        help="Verify the existing index and canonical library read-only.",
    )
    verification_group.add_argument(
        "--verify-json", action="store_true",
        help="Verify read-only and emit one machine-readable JSON report.",
    )
    args = parser.parse_args(argv)

    def require_paths(*items: tuple[str, Optional[Path]]) -> bool:
        missing = [name for name, value in items if value is None]
        if missing:
            logger.error("explicit path required: %s", ", ".join(missing))
            return False
        return True

    if args.verify or args.verify_json:
        incompatible: list[str] = []
        if args.export_wallhaven_ledger is not None:
            incompatible.append("--export-wallhaven-ledger")
        if args.enrich:
            incompatible.append("--enrich")
        if args.max_fetch is not None:
            incompatible.append("--max-fetch")
        if args.max_attempts is not None:
            incompatible.append("--max-attempts")
        if args.ledger_path is not None:
            incompatible.append("--ledger-path")
        if args.provider_ledger_path is not None:
            incompatible.append("--provider-ledger-path")
        if args.apply_provider_ledger:
            incompatible.append("--apply-provider-ledger")
        if args.env_path is not None:
            incompatible.append("--env-path")
        if args.query is not None:
            incompatible.append("--query")
        if args.stats:
            incompatible.append("--stats")
        if args.emit_plan is not None:
            incompatible.append("--emit-plan")
        if getattr(args, "dashboard", None) is not None:
            incompatible.append("--dashboard")
        if incompatible:
            logger.error(
                "%s cannot be combined with verification",
                ", ".join(incompatible),
            )
            return 2
        if not require_paths(
            ("--library-root", args.library_root),
            ("--db-path", args.db_path),
        ):
            return 2

        try:
            assert args.db_path is not None and args.library_root is not None
            report = verify_library(args.db_path, args.library_root)
        except (
            FileNotFoundError,
            NotADirectoryError,
            OSError,
            RuntimeError,
            ValueError,
            sqlite3.Error,
        ) as exc:
            report = _verification_input_error_report(
                args.db_path, args.library_root, str(exc),
            )
            exit_code = 2
        else:
            exit_code = _verification_exit_code(report)

        if args.verify_json:
            print(json.dumps(report, sort_keys=True))
        else:
            _print_verification_summary(report)
        return exit_code

    if args.export_wallhaven_ledger is not None:
        if not require_paths(("--db-path", args.db_path)):
            return 2
        try:
            assert args.db_path is not None
            export_stats = export_wallhaven_ledger(
                args.db_path, args.export_wallhaven_ledger,
            )
        except (FileNotFoundError, sqlite3.Error, OSError, ValueError) as exc:
            logger.error("Wallhaven ledger export failed: %s", exc)
            return 2
        print(f"Wallhaven ledger written to: {args.export_wallhaven_ledger}")
        for key in ("source_rows", "records", "duplicates_merged", "tags"):
            print(f"  {key:18s} {export_stats[key]}")
        return 0

    if args.apply_provider_ledger:
        incompatible = []
        if args.enrich:
            incompatible.append("--enrich")
        if args.query is not None:
            incompatible.append("--query")
        if args.stats:
            incompatible.append("--stats")
        if args.emit_plan is not None:
            incompatible.append("--emit-plan")
        if args.dashboard is not None:
            incompatible.append("--dashboard")
        if incompatible:
            logger.error(
                "%s cannot be combined with --apply-provider-ledger",
                ", ".join(incompatible),
            )
            return 2
        if not require_paths(
            ("--db-path", args.db_path),
            ("--provider-ledger-path", args.provider_ledger_path),
        ):
            return 2
        assert args.db_path is not None and args.provider_ledger_path is not None
        try:
            conn = open_index_write(args.db_path)
            try:
                stats = import_provider_ledger(conn, args.provider_ledger_path)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except (FileNotFoundError, OSError, ValueError, sqlite3.Error) as exc:
            logger.error("provider ledger import failed: %s", exc)
            return 2
        print(f"Provider ledger applied: {args.provider_ledger_path}")
        for key in sorted(stats):
            print(f"  {key:30s} {stats[key]}")
        return 0

    read_mode = any(
        (
            args.query is not None, args.stats,
            args.emit_plan is not None, args.dashboard is not None,
        )
    )
    if read_mode:
        if args.enrich:
            logger.error("--enrich cannot be combined with a read-only mode")
            return 2
        if not require_paths(("--db-path", args.db_path)):
            return 2
        if (args.emit_plan is not None or args.dashboard is not None) and not require_paths(
            ("--library-root", args.library_root),
        ):
            return 2
        assert args.db_path is not None
        try:
            conn = open_index_read_only(args.db_path)
            try:
                if args.query is not None:
                    q = _parse_query_arg(args.query)
                    seed_text = q.get("shuffle_seed") or q.get("seed")
                    rows = query(
                        conn,
                        orientation=q.get("orientation"),
                        franchise=q.get("franchise"),
                        bucket=q.get("bucket"),
                        source=q.get("source"),
                        tag=q.get("tag"),
                        content_rating=q.get("rating") or q.get("content_rating"),
                        sort=q.get("sort", "path"),
                        shuffle_seed=None if seed_text is None else int(seed_text),
                        limit=int(q.get("limit", "100")),
                        offset=int(q.get("offset", "0")),
                    )
                    for row in rows:
                        print(
                            f"{row.resolution_bucket}\t{row.orientation or '?'}\t"
                            f"{row.source}\t{row.path}"
                        )
                elif args.stats:
                    _print_stats(conn)
                elif args.emit_plan is not None:
                    assert args.library_root is not None
                    plan_stats = emit_reorg_plan(
                        conn, Path(args.emit_plan), args.library_root,
                    )
                    print(f"Plan written to: {args.emit_plan}")
                    print(f"  emitted:  {plan_stats['emitted']}")
                    print(f"  missing:  {plan_stats['missing']}")
                    print(f"  outside:  {plan_stats['outside_root']}")
                elif args.dashboard is not None:
                    assert args.library_root is not None
                    dash_stats = generate_dashboard(
                        conn, Path(args.dashboard), args.library_root,
                    )
                    print(f"Dashboard written to: {args.dashboard}")
                    print(f"  images:   {dash_stats['present']}")
                    print(f"  missing:  {dash_stats['missing']}")
                    print(f"  days:     {dash_stats['days']}")
            finally:
                conn.close()
        except (FileNotFoundError, OSError, ValueError, sqlite3.Error) as exc:
            logger.error("read-only index operation failed: %s", exc)
            return 2
        return 0

    required = [
        ("--library-root", args.library_root),
        ("--db-path", args.db_path),
        ("--ledger-path", args.ledger_path),
        ("--provider-ledger-path", args.provider_ledger_path),
    ]
    if args.enrich:
        required.append(("--env-path", args.env_path))
    if not require_paths(*required):
        return 2
    assert args.library_root is not None
    assert args.db_path is not None
    assert args.ledger_path is not None
    assert args.provider_ledger_path is not None
    try:
        conn = connect(args.db_path)
    except (OSError, ValueError, sqlite3.Error) as exc:
        logger.error("index open/migration failed: %s", exc)
        return 2

    try:
        ledger_path = args.ledger_path
        stats = ingest_library(
            conn, args.library_root, ledger_path=ledger_path,
            provider_ledger_path=args.provider_ledger_path,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        logger.error("%s", exc)
        conn.close()
        return 2
    print("Ingest complete:")
    for k in (
        "scanned", "wallhaven", "zerochan", "anime_pictures", "unknown",
        "tagged", "sidecars", "invalid_sidecars", "stale_removed",
        "ledger_lines", "ledger_records", "ledger_invalid",
        "ledger_superseded", "ledger_applied", "ledger_unmatched",
        "provider_ledger_lines", "provider_ledger_records",
        "provider_ledger_invalid", "provider_ledger_superseded",
        "provider_records_matched", "provider_records_unmatched",
        "provider_images_updated", "provider_tags_applied",
    ):
        print(f"  {k:14s} {stats[k]}")
    print(f"DB: {args.db_path}")

    if args.enrich:
        assert args.env_path is not None
        key = load_api_key(args.env_path)
        print(f"\nEnriching Wallhaven (api key: {'yes' if key else 'no (SFW only)'})...")
        est = _estimate_enrich_minutes(conn)
        print(f"  estimated runtime: ~{est} min (resumable)")
        enrich_stats = enrich_wallhaven(
            conn, key, max_fetch=args.max_fetch,
            max_attempts=args.max_attempts, ledger_path=ledger_path,
        )
        print("Enrichment pass finished:")
        for k in ("attempted", "fetched", "skipped", "failed", "remaining"):
            print(f"  {k:14s} {enrich_stats[k]}")

    conn.close()
    return 0


def _estimate_enrich_minutes(conn: sqlite3.Connection) -> int:
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM images WHERE source='wallhaven' "
        "AND enrichment_status = ?", (STATUS_PENDING,),
    ).fetchone()["c"]
    return int(pending * WALLHAVEN_SLEEP_SECONDS / 60)


def _print_stats(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) AS c FROM images").fetchone()["c"]
    print(f"Total images: {total}")
    print("\nBy source:")
    for row in conn.execute(
        "SELECT source, COUNT(*) AS c FROM images GROUP BY source ORDER BY c DESC"
    ):
        print(f"  {row['source']:16s} {row['c']}")
    print("\nBy orientation:")
    for row in conn.execute(
        "SELECT orientation, COUNT(*) AS c FROM images GROUP BY orientation "
        "ORDER BY c DESC"
    ):
        print(f"  {(row['orientation'] or 'unknown'):16s} {row['c']}")
    print("\nBy resolution bucket:")
    for row in conn.execute(
        "SELECT resolution_bucket, COUNT(*) AS c FROM images "
        "GROUP BY resolution_bucket ORDER BY c DESC"
    ):
        print(f"  {(row['resolution_bucket'] or 'unknown'):16s} {row['c']}")
    print("\nBy content rating:")
    for row in conn.execute(
        "SELECT content_rating AS rating, COUNT(*) AS c FROM images "
        "GROUP BY content_rating ORDER BY c DESC"
    ):
        print(f"  {row['rating']:16s} {row['c']}")
    print("\nTop 15 franchises:")
    for row in conn.execute(
        "SELECT franchise, COUNT(*) AS c FROM images "
        "WHERE franchise IS NOT NULL GROUP BY franchise "
        "ORDER BY c DESC LIMIT 15"
    ):
        print(f"  {row['c']:5d}  {row['franchise']}")
    print("\nEnrichment status:")
    for row in conn.execute(
        "SELECT enrichment_status, COUNT(*) AS c FROM images "
        "GROUP BY enrichment_status ORDER BY c DESC"
    ):
        print(f"  {row['enrichment_status']:16s} {row['c']}")


if __name__ == "__main__":
    sys.exit(main())
