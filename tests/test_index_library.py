"""Tests for ``dl_engine.index_library``.

These cover the offline pure-logic pieces (filename classification,
anime-pictures tag parsing, orientation/bucket derivation, the SQLite ingest
against a temp tree) plus a surface guard that mirrors
``test_common_utils.py`` and ``test_package_surface.py``: this module must
never grow move/sort/delete helpers. The Wallhaven HTTP path is exercised
manually (same convention as ``test_anime_pictures.py``).
"""
from __future__ import annotations

import io
import hashlib
import struct
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path
from typing import Optional

from dl_engine import index_library as idx
from dl_engine import wallpaper_metadata as metadata


# ---------------------------------------------------------------------------
# Filename classification
# ---------------------------------------------------------------------------


class ClassifyFilenameTests(unittest.TestCase):
    def test_wallhaven_alphanumeric(self) -> None:
        c = idx.classify_filename("wallhaven_g7xd1d_2000x2999")
        self.assertEqual(c.source, "wallhaven")
        self.assertEqual(c.source_site_id, "g7xd1d")
        self.assertEqual((c.width, c.height), (2000, 2999))
        self.assertIsNone(c.tag_suffix)

    def test_wallhaven_numeric_id(self) -> None:
        c = idx.classify_filename("wallhaven_3301538_3840x2160")
        self.assertEqual(c.source, "wallhaven")
        self.assertEqual(c.source_site_id, "3301538")
        self.assertEqual((c.width, c.height), (3840, 2160))

    def test_wallhaven_case_insensitive(self) -> None:
        c = idx.classify_filename("Wallhaven_ABC123_1920x1080")
        self.assertEqual(c.source, "wallhaven")
        self.assertEqual(c.source_site_id, "abc123")

    def test_anime_pictures_prefixed(self) -> None:
        c = idx.classify_filename(
            "ANIME-PICTURES.NET_-_922947-1800x2546-virtual+youtuber-mr.lime"
        )
        self.assertEqual(c.source, "anime-pictures")
        self.assertEqual(c.source_site_id, "922947")
        self.assertEqual((c.width, c.height), (1800, 2546))
        self.assertEqual(c.tag_suffix, "virtual+youtuber-mr.lime")

    def test_anime_pictures_ap_prefix(self) -> None:
        c = idx.classify_filename("ap-912688-5568x3132-zenless+zone+zero-ellen+joe")
        self.assertEqual(c.source, "anime-pictures")
        self.assertEqual(c.source_site_id, "912688")
        self.assertEqual(c.tag_suffix, "zenless+zone+zero-ellen+joe")

    def test_anime_pictures_preview_without_tag_suffix(self) -> None:
        c = idx.classify_filename("ap-912688-5568x3132")
        self.assertEqual(c.source, "anime-pictures")
        self.assertEqual(c.source_site_id, "912688")
        self.assertEqual((c.width, c.height), (5568, 3132))
        self.assertIsNone(c.tag_suffix)

    def test_bare_numeric_is_unknown(self) -> None:
        c = idx.classify_filename("3301538")
        self.assertEqual(c.source, "unknown")
        self.assertEqual(c.source_site_id, "3301538")
        self.assertIsNone(c.width)

    def test_bare_numeric_with_sorter_disambiguator_is_unknown(self) -> None:
        c = idx.classify_filename("3915869_0001")
        self.assertEqual(c.source, "unknown")
        self.assertEqual(c.source_site_id, "3915869")

    def test_generic_anime_prefix_is_unknown(self) -> None:
        c = idx.classify_filename("anime-wallpaper-4K-001")
        self.assertEqual(c.source, "unknown")
        self.assertIsNone(c.source_site_id)

    def test_completely_unknown_string_is_unknown(self) -> None:
        c = idx.classify_filename("some random scan")
        self.assertEqual(c.source, "unknown")
        self.assertIsNone(c.source_site_id)


# ---------------------------------------------------------------------------
# Anime-pictures tag parsing
# ---------------------------------------------------------------------------


class ParseAnimePicturesTagsTests(unittest.TestCase):
    def test_franchise_and_attributes(self) -> None:
        p = idx.parse_anime_pictures_tags(
            "genshin+impact-raiden+shogun-single-long+hair-tall+image"
        )
        self.assertEqual(p.franchise, "genshin impact")
        self.assertIn("raiden shogun", p.tags)
        self.assertIn("single", p.tags)
        self.assertIn("long hair", p.tags)
        self.assertIn("tall image", p.tags)

    def test_plus_decodes_to_space(self) -> None:
        p = idx.parse_anime_pictures_tags("virtual+youtuber-mr.lime")
        self.assertEqual(p.franchise, "virtual youtuber")
        self.assertEqual(p.tags, ["mr.lime"])

    def test_percent_encoding_decoded(self) -> None:
        # Goddess of Victory: Nikke appears with %3A for the colon in URLs.
        p = idx.parse_anime_pictures_tags("goddess+of+victory%3A+nikke-cinderella")
        self.assertEqual(p.franchise, "goddess of victory: nikke")

    def test_empty_suffix(self) -> None:
        p = idx.parse_anime_pictures_tags("")
        self.assertIsNone(p.franchise)
        self.assertEqual(p.tags, [])

    def test_only_franchise_segment(self) -> None:
        p = idx.parse_anime_pictures_tags("touhou")
        self.assertEqual(p.franchise, "touhou")
        self.assertEqual(p.tags, [])

    def test_double_decoded_segment_kept(self) -> None:
        # A segment that is just whitespace should not produce an empty tag.
        p = idx.parse_anime_pictures_tags("franchise-+-single")
        self.assertEqual(p.franchise, "franchise")
        self.assertEqual([t for t in p.tags if t], ["single"])


# ---------------------------------------------------------------------------
# Orientation + bucket
# ---------------------------------------------------------------------------


class OrientationTests(unittest.TestCase):
    def test_landscape(self) -> None:
        self.assertEqual(idx.derive_orientation(3840, 2160), "landscape")

    def test_portrait(self) -> None:
        self.assertEqual(idx.derive_orientation(2160, 3840), "portrait")

    def test_square_exact(self) -> None:
        self.assertEqual(idx.derive_orientation(4096, 4096), "square")

    def test_square_within_tolerance(self) -> None:
        # 1024x1000 is within 3% of square.
        self.assertEqual(idx.derive_orientation(1024, 1000), "square")

    def test_none_when_missing(self) -> None:
        self.assertIsNone(idx.derive_orientation(None, 1000))
        self.assertIsNone(idx.derive_orientation(1000, None))

    def test_none_when_nonpositive(self) -> None:
        self.assertIsNone(idx.derive_orientation(0, 0))


class ResolutionBucketTests(unittest.TestCase):
    def test_4k(self) -> None:
        self.assertEqual(idx.resolution_bucket_for(3840, 2160), "4K")

    def test_4k_portrait_long_side(self) -> None:
        self.assertEqual(idx.resolution_bucket_for(2160, 3840), "4K")

    def test_1440p(self) -> None:
        self.assertEqual(idx.resolution_bucket_for(2560, 1440), "1440p")

    def test_1080p(self) -> None:
        self.assertEqual(idx.resolution_bucket_for(1920, 1080), "1080p")

    def test_720p(self) -> None:
        self.assertEqual(idx.resolution_bucket_for(1280, 720), "720p")

    def test_sd(self) -> None:
        self.assertEqual(idx.resolution_bucket_for(800, 600), "SD")

    def test_none_when_missing(self) -> None:
        self.assertIsNone(idx.resolution_bucket_for(None, 100))


# ---------------------------------------------------------------------------
# Header dimension reader (uses synthetic image bytes, no real files)
# ---------------------------------------------------------------------------


def _write_png(path: Path, width: int, height: int) -> None:
    """Write a minimal valid PNG with the given dimensions."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    crc = _png_crc(b"IHDR", ihdr)
    path.write_bytes(sig + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + crc)


def _write_webp_vp8(path: Path, width: int, height: int) -> None:
    """Write a minimal RIFF/WEBP VP8 (lossy) header carrying the dims.

    Mirrors the on-disk byte layout of the library's two real VP8 WEBPs
    (e.g. 4124807.webp): RIFF(4)+riff-size(4)+WEBP(4)+'VP8 '(4)+chunk-size(4)
    then payload = 3-byte frame tag + 3 bytes + 16-bit width + 16-bit height.
    The parser reads width/height at payload offsets 6/8.
    """
    payload = b"\x10\x10\x16\x9d\x01\x2a" + struct.pack("<HH", width, height)
    chunk_size = len(payload)
    riff_size = 4 + 8 + chunk_size  # 'WEBP' + chunk header + payload
    path.write_bytes(
        b"RIFF" + struct.pack("<I", riff_size) + b"WEBP"
        + b"VP8 " + struct.pack("<I", chunk_size) + payload
    )


def _png_crc(chunk_type: bytes, data: bytes) -> bytes:
    import zlib

    return struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)


def _write_metadata_sidecar(
    image: Path,
    *,
    bucket: str = "4K",
    orientation: str = "landscape",
    tags: Optional[list[dict[str, str]]] = None,
    declared_size: Optional[int] = None,
    declared_sha: str = "b" * 64,
) -> Path:
    parsed = metadata.parse_canonical_filename(image.name)
    if parsed is None:
        raise AssertionError(f"test image is not canonical: {image.name}")
    document = {
        "schema_version": 1,
        "source": parsed.source,
        "source_id": parsed.source_id,
        "source_url": f"https://example.test/{parsed.source}/{parsed.source_id}",
        "original_filename": f"original-{parsed.source_id}{parsed.extension}",
        "canonical_filename": image.name,
        "slug": parsed.slug,
        "file": {
            "sha256": declared_sha,
            "size_bytes": image.stat().st_size if declared_size is None else declared_size,
            "extension": parsed.extension,
            "width": parsed.width,
            "height": parsed.height,
        },
        "classification": {
            "resolution_bucket": bucket,
            "orientation": orientation,
        },
        "download": {
            "transport": "queue-browser",
            "source_relative_path": f"staging/{image.name}",
            "recorded_at": "2026-07-15T12:30:00+00:00",
        },
        "tags": tags or [],
        "search_origins": ["test search"],
    }
    path = metadata.sidecar_path_for(image)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _write_canonical_fixture_image(
    root: Path,
    *,
    source_id: str = "g7xd1d",
    sha256: str = "b" * 64,
    parent: tuple[str, str, str] = ("4K", "landscape", "wallhaven"),
) -> tuple[Path, Path]:
    image_dir = root.joinpath(*parent)
    image_dir.mkdir(parents=True, exist_ok=True)
    image = image_dir / (
        f"src=wallhaven__id={source_id}__size=3840x2160__slug=fixture.jpg"
    )
    image.write_bytes(f"fixture payload {source_id}".encode("utf-8"))
    sidecar = _write_metadata_sidecar(image, declared_sha=sha256)
    return image, sidecar


def _create_verified_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    root = tmp_path / "library"
    image, sidecar = _write_canonical_fixture_image(root)
    db = tmp_path / "index.sqlite"
    conn = idx.connect(db)
    try:
        idx.ingest_library(conn, root)
    finally:
        conn.close()
    return root, db, image, sidecar


def _clone_indexed_image_row(
    db: Path, source_path: Path, target_path: Path,
) -> None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(images)")
            if row[1] != "id"
        ]
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM images WHERE path = ?",
            (str(source_path.resolve()),),
        ).fetchone()
        if row is None:
            raise AssertionError(f"fixture row not found: {source_path}")
        values = dict(row)
        values["path"] = str(target_path.resolve())
        values["filename"] = target_path.name
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO images ({', '.join(columns)}) VALUES ({placeholders})",
            [values[column] for column in columns],
        )
        conn.commit()
    finally:
        conn.close()


def _insert_minimal_image(
    conn: sqlite3.Connection,
    number: int,
    *,
    source: str = "zerochan",
    source_id: Optional[str] = None,
    purity: Optional[str] = None,
    enrichment_status: str = "pending",
    recorded_at: Optional[str] = None,
) -> int:
    row = conn.execute(
        "INSERT INTO images("
        "path,filename,source,source_site_id,purity,enrichment_status,indexed_at,"
        "download_recorded_at,width,height,orientation,resolution_bucket) "
        "VALUES (?,?,?,?,?,?,?, ?,1920,1080,'landscape','1080p') RETURNING id",
        (
            f"C:/synthetic/{number}.jpg", f"{number}.jpg", source,
            source_id or str(number), purity, enrichment_status,
            "2026-07-20T00:00:00+00:00",
            recorded_at or f"2026-07-{(number % 20) + 1:02d}T00:00:00+00:00",
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _create_schema2_fixture(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE images (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL, source TEXT NOT NULL, ext TEXT,
            width INTEGER, height INTEGER, orientation TEXT,
            resolution_bucket TEXT, source_site_id TEXT, franchise TEXT,
            purity TEXT, enrichment_status TEXT NOT NULL, indexed_at TEXT NOT NULL,
            metadata_path TEXT, source_url TEXT, original_filename TEXT,
            canonical_filename TEXT, slug TEXT, sha256 TEXT, size_bytes INTEGER,
            transport TEXT, source_relative_path TEXT, download_recorded_at TEXT,
            search_origins_json TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE tags (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, category_id INTEGER,
            category TEXT, source TEXT, slug TEXT, tag_type TEXT, provenance TEXT
        );
        CREATE TABLE image_tags (
            image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            provenance TEXT, PRIMARY KEY(image_id,tag_id)
        );
        CREATE TABLE enrichment_progress (
            source TEXT PRIMARY KEY, last_processed_source_site_id TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE schema_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO images(
            path,filename,source,source_site_id,purity,enrichment_status,indexed_at
        ) VALUES ('C:/v2/one.jpg','one.jpg','wallhaven','aa1',NULL,'pending','now');
        INSERT INTO tags(id,name,source,slug,tag_type,provenance)
        VALUES (-9001,'hentai','wallhaven','hentai','rating','wallhaven-api');
        INSERT INTO image_tags VALUES (1,-9001,'wallhaven-api');
        INSERT INTO enrichment_progress VALUES ('wallhaven','zz9','before');
        INSERT INTO schema_metadata VALUES ('schema_version','2');
        PRAGMA user_version=2;
        """
    )
    conn.commit()
    conn.close()


def _create_schema3_fixture(db: Path) -> None:
    """Create the previous rebuildable gallery schema without v4 fields."""

    _create_schema2_fixture(db)
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        ALTER TABLE images ADD COLUMN content_rating TEXT NOT NULL DEFAULT 'unknown'
            CHECK(content_rating IN ('sfw','suggestive','nsfw','unknown'));
        ALTER TABLE images ADD COLUMN rating_confidence REAL NOT NULL DEFAULT 0
            CHECK(rating_confidence >= 0 AND rating_confidence <= 1);
        ALTER TABLE images ADD COLUMN rating_basis TEXT NOT NULL DEFAULT 'no-signal';
        ALTER TABLE images ADD COLUMN rating_reasons_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE images ADD COLUMN tag_count INTEGER NOT NULL DEFAULT 0
            CHECK(tag_count >= 0);
        CREATE TABLE library_facets (
            facet TEXT NOT NULL, value TEXT NOT NULL, count INTEGER NOT NULL,
            refreshed_at TEXT NOT NULL, PRIMARY KEY(facet,value)
        );
        CREATE TABLE tag_suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
            label TEXT NOT NULL, normalized_label TEXT NOT NULL,
            confidence REAL NOT NULL, generator TEXT NOT NULL,
            model_version TEXT NOT NULL, provenance TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
            reviewed_at TEXT, reviewer TEXT, decision_note TEXT,
            UNIQUE(image_id,normalized_label,generator,model_version)
        );
        UPDATE schema_metadata SET value='3' WHERE key='schema_version';
        PRAGMA user_version=3;
        """
    )
    conn.commit()
    conn.close()


def _wallhaven_ledger_record(
    source_id: str,
    *,
    status: str = "ok",
    franchise: Optional[str] = "Blue Archive",
    purity: Optional[str] = "sfw",
    tags: Optional[list[dict[str, object]]] = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_type": "wallhaven-enrichment",
        "source": "wallhaven",
        "source_id": source_id,
        "enrichment_status": status,
        "franchise": franchise,
        "purity": purity,
        "provenance": "wallhaven-api",
        "tags": tags or [],
    }


class ReadDimensionsTests(unittest.TestCase):
    def test_png_header(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            path = Path(tf.name)
        try:
            _write_png(path, 2000, 3000)
            self.assertEqual(idx.read_image_dimensions(path), (2000, 3000))
        finally:
            path.unlink(missing_ok=True)

    def test_missing_file_returns_none(self) -> None:
        self.assertEqual(
            idx.read_image_dimensions(Path("__does_not_exist__.png")),
            (None, None),
        )

    def test_webp_vp8_lossy_header(self) -> None:
        # Regression for the two real VP8 WEBPs (4124807.webp / 4124810.webp)
        # that were returning (None, None) before the VP8 parser was added.
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as tf:
            path = Path(tf.name)
        try:
            _write_webp_vp8(path, 3840, 2160)
            self.assertEqual(idx.read_image_dimensions(path), (3840, 2160))
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# SQLite ingest against a temp tree
# ---------------------------------------------------------------------------


class SQLiteConcurrencyTests(unittest.TestCase):
    def test_writer_uses_wal_and_readers_survive_exclusive_transaction(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite"
            writer = idx.connect(db)
            reader = None
            try:
                writer.execute(
                    "INSERT INTO images ("
                    "path, filename, source, enrichment_status, indexed_at"
                    ") VALUES ('before.jpg', 'before.jpg', 'test', 'ok', 'now')"
                )
                writer.commit()
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode").fetchone()[0],
                    "wal",
                )

                writer.execute("BEGIN EXCLUSIVE")
                writer.execute(
                    "UPDATE images SET filename='after.jpg' WHERE path='before.jpg'"
                )
                reader = idx.open_index_read_only(db)
                visible = reader.execute(
                    "SELECT filename FROM images WHERE path='before.jpg'"
                ).fetchone()[0]
                self.assertEqual(visible, "before.jpg")
            finally:
                if reader is not None:
                    reader.close()
                if writer.in_transaction:
                    writer.rollback()
                writer.close()


class IngestLibraryTests(unittest.TestCase):
    def _make_tree(self, tmp_path: Path) -> Path:
        """Create a tiny fake library under tmp_path/images."""
        root = tmp_path / "images"
        (root / "4K").mkdir(parents=True)
        (root / "1080p").mkdir(parents=True)
        (root / "SD").mkdir(parents=True)

        # Wallhaven portrait (filename carries dims).
        (root / "4K" / "wallhaven_abc123_2160x3840.jpg").write_bytes(b"x")
        # Anime-pictures tagged landscape.
        (root / "1080p" / "922947-1800x2546-virtual+youtuber-mr.lime.jpg").write_bytes(b"x")
        # Bare numeric unknown — no dims in filename; write a real PNG so the
        # header reader fills them in.
        unknown_png = root / "SD" / "4650930.png"
        _write_png(unknown_png, 700, 953)
        # A non-image file that must be ignored.
        (root / "4K" / "notes.txt").write_text("ignore me", encoding="utf-8")
        return root

    def test_ingest_classifies_and_counts(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            db = Path(tmp) / "index.sqlite"
            conn = idx.connect(db)
            try:
                stats = idx.ingest_library(conn, root)
                self.assertEqual(stats["scanned"], 3)
                self.assertEqual(stats["wallhaven"], 1)
                self.assertEqual(stats["anime_pictures"], 1)
                self.assertEqual(stats["unknown"], 1)
                self.assertEqual(stats["tagged"], 1)  # only anime-pictures
            finally:
                conn.close()

    def test_ingest_sets_orientation_and_bucket(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                wh = conn.execute(
                    "SELECT orientation, resolution_bucket, source, width, height "
                    "FROM images WHERE source = 'wallhaven'"
                ).fetchone()
                self.assertEqual(wh["orientation"], "portrait")
                self.assertEqual(wh["resolution_bucket"], "4K")
                self.assertEqual((wh["width"], wh["height"]), (2160, 3840))

                ap = conn.execute(
                    "SELECT orientation, franchise, enrichment_status "
                    "FROM images WHERE source = 'anime-pictures'"
                ).fetchone()
                self.assertEqual(ap["orientation"], "portrait")
                self.assertEqual(ap["franchise"], "virtual youtuber")
                self.assertEqual(ap["enrichment_status"], "ok")

                unk = conn.execute(
                    "SELECT orientation, source_site_id, enrichment_status "
                    "FROM images WHERE source = 'unknown'"
                ).fetchone()
                self.assertEqual(unk["orientation"], "portrait")
                self.assertEqual(unk["source_site_id"], "4650930")
                self.assertEqual(unk["enrichment_status"], "skipped")
            finally:
                conn.close()

    def test_ingest_writes_anime_pictures_tags(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                tags = {
                    r["name"]
                    for r in conn.execute(
                        "SELECT t.name FROM image_tags it "
                        "JOIN tags t ON t.id = it.tag_id "
                        "JOIN images i ON i.id = it.image_id "
                        "WHERE i.source = 'anime-pictures'"
                    )
                }
                self.assertIn("virtual youtuber", tags)
                self.assertIn("mr.lime", tags)
            finally:
                conn.close()

    def test_ingest_idempotent(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            db = Path(tmp) / "index.sqlite"
            conn = idx.connect(db)
            try:
                idx.ingest_library(conn, root)
                idx.ingest_library(conn, root)  # re-run
                total = conn.execute("SELECT COUNT(*) AS c FROM images").fetchone()["c"]
                self.assertEqual(total, 3)
            finally:
                conn.close()



    def test_reingest_preserves_enrichment_status(self) -> None:
        """Re-ingest must NOT reset enrichment_status / franchise / purity.
        Regression: the ingest UPSERT used to overwrite enrichment_status with
        the fresh initial value (pending/ok/skipped) on every run, which wiped
        all wallhaven enrichment progress each time ``--enrich`` re-ingested.
        Enrichment-derived columns (enrichment_status, franchise, purity) must
        be preserved for rows already processed.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            db = Path(tmp) / "index.sqlite"
            conn = idx.connect(db)
            try:
                idx.ingest_library(conn, root)
                # Simulate the enrichment pass having processed the wallhaven row.
                conn.execute(
                    "UPDATE images SET enrichment_status = 'ok', "
                    "franchise = 'synthetic series', purity = 'sfw' "
                    "WHERE source = 'wallhaven'"
                )
                conn.commit()
                # Re-ingest (as --enrich does before its API pass).
                idx.ingest_library(conn, root)
                wh = conn.execute(
                    "SELECT enrichment_status, franchise, purity "
                    "FROM images WHERE source = 'wallhaven'"
                ).fetchone()
                self.assertEqual(wh["enrichment_status"], "ok",
                                 "re-ingest must not reset enrichment_status")
                self.assertEqual(wh["franchise"], "synthetic series",
                                 "re-ingest must not clobber enrichment franchise")
                self.assertEqual(wh["purity"], "sfw",
                                 "re-ingest must not clobber enrichment purity")
                # Other wallhaven rows that were never enriched still start pending.
                # (Here there's only one wallhaven row, so check the unknown/anime
                # rows retained their initial statuses too.)
                ap = conn.execute(
                    "SELECT enrichment_status FROM images WHERE source = 'anime-pictures'"
                ).fetchone()
                self.assertEqual(ap["enrichment_status"], "ok")
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Canonical recursive ingest / metadata authority / deterministic identity
# ---------------------------------------------------------------------------


class CanonicalIngestTests(unittest.TestCase):
    def test_live_capable_cli_paths_are_explicit(self) -> None:
        self.assertFalse(hasattr(idx, "DEFAULT_LIBRARY_ROOT"))
        self.assertFalse(hasattr(idx, "DEFAULT_DB_PATH"))
        self.assertEqual(idx.main([]), 2)

    def test_missing_library_root_fails_loudly(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                with self.assertRaises(FileNotFoundError):
                    idx.ingest_library(conn, Path(tmp) / "missing-library")
            finally:
                conn.close()

    def test_recursive_post_orientation_ingest(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            image_dir = root / "4K" / "portrait" / "wallhaven"
            image_dir.mkdir(parents=True)
            image = image_dir / "wallhaven_abcd1_2160x3840.jpg"
            image.write_bytes(b"x")
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                stats = idx.ingest_library(conn, root)
                self.assertEqual(stats["scanned"], 1)
                row = conn.execute("SELECT * FROM images").fetchone()
                self.assertEqual(row["source"], "wallhaven")
                self.assertEqual(row["resolution_bucket"], "4K")
                self.assertEqual(row["orientation"], "portrait")
            finally:
                conn.close()

    def test_reconcile_deletes_stale_rows_for_scanned_root(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            image_dir = root / "SD" / "landscape" / "unknown"
            image_dir.mkdir(parents=True)
            image = image_dir / "3301538.png"
            _write_png(image, 800, 600)
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                image.unlink()
                stats = idx.ingest_library(conn, root)
                self.assertEqual(stats["stale_removed"], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) AS c FROM images").fetchone()["c"], 0,
                )
            finally:
                conn.close()

    def test_valid_sidecar_is_authoritative_and_query_loads_typed_tags(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            image_dir = root / "SD" / "landscape" / "anime-pictures"
            image_dir.mkdir(parents=True)
            image = image_dir / (
                "src=anime-pictures__id=921265__size=3986x2304"
                "__slug=azur-lane-evertsen.png"
            )
            image.write_bytes(b"image payload")
            _write_metadata_sidecar(
                image,
                tags=[
                    {
                        "name": "Azur Lane", "slug": "azur-lane",
                        "type": "franchise", "provenance": "anime-pictures-url",
                    },
                    {
                        "name": "Evertsen", "slug": "evertsen",
                        "type": "character", "provenance": "anime-pictures-url",
                    },
                ],
            )
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                stats = idx.ingest_library(conn, root)
                self.assertEqual(stats["sidecars"], 1)
                rows = idx.query(conn, source="anime_pictures", tag="Evertsen")
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row.resolution_bucket, "4K")
                self.assertEqual(row.transport, "queue-browser")
                self.assertEqual(row.search_origins, ("test search",))
                self.assertEqual(row.franchise, "Azur Lane")
                self.assertEqual(
                    {(tag.name, tag.type, tag.provenance) for tag in row.tags},
                    {
                        ("Azur Lane", "franchise", "anime-pictures-url"),
                        ("Evertsen", "character", "anime-pictures-url"),
                    },
                )
            finally:
                conn.close()

    def test_stale_sibling_sidecar_falls_back_to_filename(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            image_dir = root / "4K" / "portrait" / "wallhaven"
            image_dir.mkdir(parents=True)
            image = image_dir / (
                "src=wallhaven__id=g7xd1d__size=2000x2999__slug=genshin-impact.jpg"
            )
            image.write_bytes(b"actual")
            _write_metadata_sidecar(
                image, bucket="4K", orientation="portrait", declared_size=999,
            )
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                stats = idx.ingest_library(conn, root)
                self.assertEqual(stats["invalid_sidecars"], 1)
                self.assertEqual(stats["sidecars"], 0)
                row = conn.execute("SELECT * FROM images").fetchone()
                self.assertEqual(row["source"], "wallhaven")
                self.assertIsNone(row["metadata_path"])
                self.assertIsNone(row["source_url"])
            finally:
                conn.close()

    def test_sidecar_for_a_different_canonical_filename_is_rejected(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            image_dir = root / "4K" / "portrait" / "wallhaven"
            image_dir.mkdir(parents=True)
            image = image_dir / (
                "src=wallhaven__id=g7xd1d__size=2000x2999__slug=genshin-impact.jpg"
            )
            image.write_bytes(b"actual")
            sidecar = _write_metadata_sidecar(
                image, bucket="4K", orientation="portrait",
            )
            document = json.loads(sidecar.read_text(encoding="utf-8"))
            document["source_id"] = "zzzz9"
            document["slug"] = "different-image"
            document["canonical_filename"] = (
                "src=wallhaven__id=zzzz9__size=2000x2999__slug=different-image.jpg"
            )
            sidecar.write_text(json.dumps(document), encoding="utf-8")
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                stats = idx.ingest_library(conn, root)
                self.assertEqual(stats["invalid_sidecars"], 1)
                self.assertIsNone(
                    conn.execute("SELECT metadata_path FROM images").fetchone()["metadata_path"]
                )
            finally:
                conn.close()

    def test_unreadable_legacy_image_is_total_and_routable(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            (root / "4K").mkdir(parents=True)
            (root / "4K" / "unreadable.jpg").write_bytes(b"not an image")
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                row = conn.execute(
                    "SELECT source, width, height, resolution_bucket, orientation "
                    "FROM images"
                ).fetchone()
                self.assertEqual(
                    tuple(row),
                    ("unknown", 0, 0, "_UnknownResolution", "unknown"),
                )
                destination, orientation = idx.destination_for(row)
                self.assertEqual(
                    (destination, orientation),
                    ("_UnknownResolution/unknown/unknown", "unknown"),
                )
            finally:
                conn.close()

    def test_moved_identity_updates_path_without_duplicate_row(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            old_dir = root / "4K"
            old_dir.mkdir(parents=True)
            image = old_dir / "wallhaven_abcd1_2160x3840.jpg"
            image.write_bytes(b"x")
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                new_dir = root / "4K" / "portrait" / "wallhaven"
                new_dir.mkdir(parents=True)
                moved = image.rename(new_dir / image.name)
                stats = idx.ingest_library(conn, root)
                rows = conn.execute("SELECT path FROM images").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(Path(rows[0]["path"]), moved.resolve())
                self.assertEqual(stats["stale_removed"], 0)
            finally:
                conn.close()

    def test_exact_duplicate_quarantine_is_excluded(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            duplicate_dir = root / "_ExactDuplicates" / "wallhaven"
            duplicate_dir.mkdir(parents=True)
            (duplicate_dir / "wallhaven_abcd1_2160x3840.jpg").write_bytes(b"x")
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                stats = idx.ingest_library(conn, root)
                self.assertEqual(stats["scanned"], 0)
            finally:
                conn.close()


class VerifyLibraryTests(unittest.TestCase):
    def test_healthy_fixture_is_ok_and_database_is_unchanged(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, _sidecar = _create_verified_fixture(Path(tmp))
            before_bytes = db.read_bytes()
            before_mtime = db.stat().st_mtime_ns

            report = idx.verify_library(db, root)

            self.assertTrue(report["ok"])
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["quick_check"], ["ok"])
            self.assertEqual(report["issues_total"], 0)
            self.assertEqual(report["issues"], [])
            self.assertEqual(report["counts"]["disk_images"], 1)
            self.assertEqual(report["counts"]["indexed_images"], 1)
            self.assertEqual(db.read_bytes(), before_bytes)
            self.assertEqual(db.stat().st_mtime_ns, before_mtime)

    def test_json_cli_shape_and_zero_one_two_exit_codes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, image, _sidecar = _create_verified_fixture(Path(tmp))

            healthy_stdout = io.StringIO()
            with redirect_stdout(healthy_stdout):
                healthy_code = idx.main([
                    "--verify-json", "--library-root", str(root),
                    "--db-path", str(db),
                ])
            healthy = json.loads(healthy_stdout.getvalue())
            self.assertEqual(healthy_code, 0)
            self.assertEqual(
                set(healthy),
                {
                    "schema_version", "ok", "status", "library_root",
                    "db_path", "quick_check", "counts", "issues_total",
                    "issues_truncated", "issues",
                },
            )
            self.assertTrue(healthy["ok"])

            image.unlink()
            failed_stdout = io.StringIO()
            with redirect_stdout(failed_stdout):
                failed_code = idx.main([
                    "--verify-json", "--library-root", str(root),
                    "--db-path", str(db),
                ])
            failed = json.loads(failed_stdout.getvalue())
            self.assertEqual(failed_code, 1)
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["counts"]["missing_indexed_paths"], 1)

            input_stdout = io.StringIO()
            with redirect_stdout(input_stdout):
                input_code = idx.main([
                    "--verify-json", "--library-root", str(root),
                    "--db-path", str(Path(tmp) / "missing.sqlite"),
                ])
            input_report = json.loads(input_stdout.getvalue())
            self.assertEqual(input_code, 2)
            self.assertEqual(input_report["issues"][0]["code"], "input-error")

    def test_human_cli_summary_and_invalid_mode_combination(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, _sidecar = _create_verified_fixture(Path(tmp))
            output = io.StringIO()
            with redirect_stdout(output):
                code = idx.main([
                    "--verify", "--library-root", str(root),
                    "--db-path", str(db),
                ])
            self.assertEqual(code, 0)
            self.assertIn("Verification: ok", output.getvalue())
            self.assertIn("Issues: 0", output.getvalue())
            self.assertEqual(
                idx.main([
                    "--verify", "--stats", "--library-root", str(root),
                    "--db-path", str(db),
                ]),
                2,
            )

    def test_missing_indexed_path_and_unindexed_disk_image(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, image, _sidecar = _create_verified_fixture(Path(tmp))
            image.unlink()
            missing_report = idx.verify_library(db, root)
            self.assertEqual(
                missing_report["counts"]["missing_indexed_paths"], 1,
            )
            self.assertIn(
                "missing-indexed-path",
                {issue["code"] for issue in missing_report["issues"]},
            )

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, _sidecar = _create_verified_fixture(Path(tmp))
            _write_canonical_fixture_image(
                root, source_id="abcd2", sha256="c" * 64,
            )
            unindexed_report = idx.verify_library(db, root)
            self.assertEqual(
                unindexed_report["counts"]["unindexed_disk_images"], 1,
            )
            self.assertIn(
                "unindexed-disk-image",
                {issue["code"] for issue in unindexed_report["issues"]},
            )

    def test_outside_root_and_quarantine_rows_are_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root, db, image, _sidecar = _create_verified_fixture(tmp_path)
            outside = tmp_path / "outside" / image.name
            outside.parent.mkdir()
            outside.write_bytes(b"outside")
            _clone_indexed_image_row(db, image, outside)

            quarantine = root / "_ExactDuplicates" / "wallhaven" / image.name
            quarantine.parent.mkdir(parents=True)
            quarantine.write_bytes(b"quarantine")
            _clone_indexed_image_row(db, image, quarantine)

            report = idx.verify_library(db, root)
            self.assertEqual(report["counts"]["outside_root_paths"], 1)
            self.assertEqual(report["counts"]["quarantine_rows"], 1)
            self.assertIn(
                "outside-root-path", {issue["code"] for issue in report["issues"]},
            )
            self.assertIn(
                "indexed-quarantine-file",
                {issue["code"] for issue in report["issues"]},
            )

    def test_missing_and_invalid_sidecars_are_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, sidecar = _create_verified_fixture(Path(tmp))
            sidecar.unlink()
            report = idx.verify_library(db, root)
            self.assertEqual(report["counts"]["missing_sidecars"], 1)

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, sidecar = _create_verified_fixture(Path(tmp))
            sidecar.write_text("{}", encoding="utf-8")
            report = idx.verify_library(db, root)
            self.assertEqual(report["counts"]["invalid_sidecars"], 1)

    def test_database_metadata_and_layout_mismatches_are_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, _sidecar = _create_verified_fixture(Path(tmp))
            conn = sqlite3.connect(str(db))
            try:
                conn.execute("UPDATE images SET source_site_id='wrong-id'")
                conn.commit()
            finally:
                conn.close()
            report = idx.verify_library(db, root)
            self.assertEqual(report["counts"]["metadata_mismatches"], 1)
            self.assertIn(
                "metadata-mismatch", {issue["code"] for issue in report["issues"]},
            )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "library"
            _write_canonical_fixture_image(
                root, parent=("4K", "portrait", "wallhaven"),
            )
            db = tmp_path / "index.sqlite"
            conn = idx.connect(db)
            try:
                idx.ingest_library(conn, root)
            finally:
                conn.close()
            report = idx.verify_library(db, root)
            self.assertEqual(report["counts"]["layout_mismatches"], 1)
            self.assertIn(
                "layout-mismatch", {issue["code"] for issue in report["issues"]},
            )

    def test_consistently_wrong_classification_is_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "library"
            _image, sidecar = _write_canonical_fixture_image(
                root, parent=("1440p", "portrait", "wallhaven"),
            )
            document = json.loads(sidecar.read_text(encoding="utf-8"))
            document["classification"] = {
                "resolution_bucket": "1440p",
                "orientation": "portrait",
            }
            sidecar.write_text(json.dumps(document), encoding="utf-8")
            db = tmp_path / "index.sqlite"
            conn = idx.connect(db)
            try:
                idx.ingest_library(conn, root)
            finally:
                conn.close()

            report = idx.verify_library(db, root)

            self.assertFalse(report["ok"])
            self.assertEqual(report["counts"]["metadata_mismatches"], 1)
            self.assertEqual(report["counts"]["layout_mismatches"], 0)
            mismatch = next(
                issue for issue in report["issues"]
                if issue["code"] == "metadata-mismatch"
            )
            self.assertIn(
                "sidecar resolution_bucket vs dimensions", mismatch["detail"],
            )
            self.assertIn(
                "sidecar orientation vs dimensions", mismatch["detail"],
            )

    def test_duplicate_nonempty_sha256_group_is_rejected(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "library"
            _write_canonical_fixture_image(root, source_id="g7xd1d")
            _write_canonical_fixture_image(root, source_id="abcd2")
            db = tmp_path / "index.sqlite"
            conn = idx.connect(db)
            try:
                idx.ingest_library(conn, root)
            finally:
                conn.close()

            report = idx.verify_library(db, root)
            self.assertEqual(report["counts"]["duplicate_sha_groups"], 1)
            duplicate = next(
                issue for issue in report["issues"]
                if issue["code"] == "duplicate-sha256"
            )
            self.assertIn("sha256=" + "b" * 64, duplicate["detail"])

    def test_schema_version_failure_is_cli_input_class_two(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, _sidecar = _create_verified_fixture(Path(tmp))
            conn = sqlite3.connect(str(db))
            try:
                conn.execute("PRAGMA user_version = 999")
                conn.commit()
            finally:
                conn.close()

            report = idx.verify_library(db, root)
            self.assertGreater(report["counts"]["schema_failures"], 0)
            output = io.StringIO()
            with redirect_stdout(output):
                code = idx.main([
                    "--verify-json", "--library-root", str(root),
                    "--db-path", str(db),
                ])
            self.assertEqual(code, 2)
            self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_foreign_key_violation_is_reported_as_invariant_failure(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, _sidecar = _create_verified_fixture(Path(tmp))
            conn = sqlite3.connect(str(db))
            try:
                conn.execute(
                    "INSERT INTO image_tags(image_id, tag_id, provenance) "
                    "VALUES (99999, 99999, 'fixture')"
                )
                conn.commit()
            finally:
                conn.close()

            report = idx.verify_library(db, root)
            self.assertGreater(report["counts"]["foreign_key_violations"], 0)
            self.assertEqual(idx._verification_exit_code(report), 1)
            self.assertIn(
                "foreign-key-violation",
                {issue["code"] for issue in report["issues"]},
            )

    def test_quick_check_failure_is_reported(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, _sidecar = _create_verified_fixture(Path(tmp))
            real_connection = idx._open_sqlite_read_only(db)

            class QuickCheckProxy:
                def execute(self, sql: str, *args: object) -> object:
                    if sql == "PRAGMA quick_check":
                        return mock.Mock(
                            fetchall=mock.Mock(
                                return_value=[("database disk image is malformed",)]
                            )
                        )
                    return real_connection.execute(sql, *args)

                def close(self) -> None:
                    real_connection.close()

            with mock.patch.object(
                idx, "_open_sqlite_read_only", return_value=QuickCheckProxy(),
            ):
                report = idx.verify_library(db, root)
            self.assertEqual(report["counts"]["quick_check_failures"], 1)
            self.assertIn(
                "sqlite-quick-check", {issue["code"] for issue in report["issues"]},
            )

    def test_issue_details_are_deterministically_truncated(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "library"
            image_dir = root / "4K" / "landscape" / "wallhaven"
            image_dir.mkdir(parents=True)
            for number in range(205):
                (image_dir / f"loose-{number:03d}.jpg").write_bytes(b"x")
            db = tmp_path / "index.sqlite"
            conn = idx.connect(db)
            conn.close()

            first = idx.verify_library(db, root)
            second = idx.verify_library(db, root)
            self.assertEqual(first["counts"]["unindexed_disk_images"], 205)
            self.assertEqual(first["counts"]["missing_sidecars"], 205)
            self.assertEqual(first["issues_total"], 410)
            self.assertEqual(len(first["issues"]), idx.VERIFY_MAX_ISSUES)
            self.assertTrue(first["issues_truncated"])
            self.assertEqual(first["issues"], second["issues"])


class DeterministicTagIdentityTests(unittest.TestCase):
    def test_id_is_stable_across_process_hash_seeds(self) -> None:
        script = (
            "from dl_engine.index_library import _stable_tag_id; "
            "print(_stable_tag_id('anime-pictures','Azur Lane','franchise'))"
        )
        outputs = []
        for seed in ("1", "987654"):
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = seed
            outputs.append(
                subprocess.check_output(
                    [sys.executable, "-c", script],
                    cwd=Path(__file__).parents[1], env=env, text=True,
                ).strip()
            )
        self.assertEqual(outputs[0], outputs[1])

    def test_identity_is_source_and_type_qualified(self) -> None:
        base = idx._stable_tag_id("anime-pictures", "shared", "character")
        self.assertNotEqual(base, idx._stable_tag_id("zerochan", "shared", "character"))
        self.assertNotEqual(base, idx._stable_tag_id("anime-pictures", "shared", "series"))

    def test_independent_databases_store_same_tag_id(self) -> None:
        import tempfile
        ids = []
        with tempfile.TemporaryDirectory() as tmp:
            for number in (1, 2):
                root = Path(tmp) / f"library-{number}" / "1080p"
                root.mkdir(parents=True)
                (root / "922947-1800x2546-virtual+youtuber-mr.lime.jpg").write_bytes(b"x")
                conn = idx.connect(Path(tmp) / f"index-{number}.sqlite")
                try:
                    idx.ingest_library(conn, root.parent)
                    ids.append(
                        conn.execute(
                            "SELECT id FROM tags WHERE name='virtual youtuber'"
                        ).fetchone()["id"]
                    )
                finally:
                    conn.close()
        self.assertEqual(ids[0], ids[1])

    def test_provenance_is_preserved_per_image_association(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library" / "4K" / "landscape" / "wallhaven"
            root.mkdir(parents=True)
            images = [
                root / "src=wallhaven__id=aaaa1__size=3840x2160__slug=first.jpg",
                root / "src=wallhaven__id=bbbb2__size=3840x2160__slug=second.jpg",
            ]
            provenances = ("origin-one", "origin-two")
            for image, provenance in zip(images, provenances):
                image.write_bytes(b"same sized payload")
                _write_metadata_sidecar(
                    image,
                    tags=[
                        {
                            "name": "Shared Search",
                            "slug": "shared-search",
                            "type": "search",
                            "provenance": provenance,
                        }
                    ],
                )
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root.parents[2])
                rows = {row.source_site_id: row for row in idx.query(conn)}
                self.assertEqual(rows["aaaa1"].tags[0].provenance, "origin-one")
                self.assertEqual(rows["bbbb2"].tags[0].provenance, "origin-two")
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) AS c FROM tags WHERE name='Shared Search'"
                    ).fetchone()["c"],
                    1,
                )
            finally:
                conn.close()


class SchemaMigrationTests(unittest.TestCase):
    def test_legacy_source_and_random_tag_id_are_migrated(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "legacy.sqlite"
            legacy = sqlite3.connect(db)
            legacy.executescript(
                """
                CREATE TABLE images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    source TEXT NOT NULL,
                    ext TEXT, width INTEGER, height INTEGER, orientation TEXT,
                    resolution_bucket TEXT, source_site_id TEXT,
                    franchise TEXT, purity TEXT,
                    enrichment_status TEXT NOT NULL, indexed_at TEXT NOT NULL
                );
                CREATE TABLE tags (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    category_id INTEGER, category TEXT
                );
                CREATE TABLE image_tags (
                    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    PRIMARY KEY (image_id, tag_id)
                );
                CREATE TABLE enrichment_progress (
                    source TEXT PRIMARY KEY,
                    last_processed_source_site_id TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO images (
                    path, filename, source, enrichment_status, indexed_at
                ) VALUES ('C:/legacy/a.jpg', 'a.jpg', 'anime_pictures', 'ok', 'now');
                INSERT INTO tags(id, name) VALUES (-12345, 'Azur Lane');
                INSERT INTO image_tags(image_id, tag_id) VALUES (1, -12345);
                """
            )
            legacy.commit()
            legacy.close()

            conn = idx.connect(db)
            try:
                image = conn.execute("SELECT source FROM images").fetchone()
                tag = conn.execute(
                    "SELECT id, source, tag_type FROM tags"
                ).fetchone()
                self.assertEqual(image["source"], "anime-pictures")
                self.assertEqual(tag["source"], "anime-pictures")
                self.assertEqual(tag["tag_type"], "unknown")
                self.assertEqual(
                    tag["id"],
                    idx._stable_tag_id("anime-pictures", "Azur Lane", "unknown"),
                )
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
            finally:
                conn.close()

    def test_legacy_wallhaven_api_tags_survive_sidecar_ingest(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "library"
            image_dir = root / "4K" / "landscape" / "wallhaven"
            image_dir.mkdir(parents=True)
            image = image_dir / (
                "src=wallhaven__id=g7xd1d__size=3840x2160__slug=blue-archive.jpg"
            )
            image.write_bytes(b"legacy wallhaven payload")
            _write_metadata_sidecar(
                image,
                tags=[
                    {
                        "name": "manual search",
                        "slug": "manual-search",
                        "type": "search",
                        "provenance": "staging-directory",
                    }
                ],
            )

            db = tmp_path / "legacy.sqlite"
            legacy = sqlite3.connect(db)
            legacy.executescript(
                """
                CREATE TABLE images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    source TEXT NOT NULL,
                    ext TEXT, width INTEGER, height INTEGER, orientation TEXT,
                    resolution_bucket TEXT, source_site_id TEXT,
                    franchise TEXT, purity TEXT,
                    enrichment_status TEXT NOT NULL, indexed_at TEXT NOT NULL
                );
                CREATE TABLE tags (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    category_id INTEGER, category TEXT
                );
                CREATE TABLE image_tags (
                    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                    PRIMARY KEY (image_id, tag_id)
                );
                CREATE TABLE enrichment_progress (
                    source TEXT PRIMARY KEY,
                    last_processed_source_site_id TEXT,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO images (
                    path, filename, source, source_site_id, franchise, purity,
                    enrichment_status, indexed_at
                ) VALUES (
                    'C:/legacy/wallhaven_g7xd1d_3840x2160.jpg',
                    'wallhaven_g7xd1d_3840x2160.jpg', 'wallhaven', 'g7xd1d',
                    'Blue Archive', 'sfw', 'ok', 'now'
                );
                INSERT INTO tags(id, name, category_id, category)
                VALUES (123, 'Blue Archive', 1, 'Games');
                INSERT INTO image_tags(image_id, tag_id) VALUES (1, 123);
                """
            )
            legacy.commit()
            legacy.close()

            conn = idx.connect(db)
            try:
                migrated = conn.execute(
                    "SELECT provenance FROM image_tags"
                ).fetchone()
                self.assertEqual(migrated["provenance"], "wallhaven-api")

                idx.ingest_library(conn, root)
                indexed = idx.query(conn, source="wallhaven")[0]
                self.assertEqual(indexed.path, str(image.resolve()))
                self.assertEqual(indexed.enrichment_status, idx.STATUS_OK)
                self.assertEqual(indexed.franchise, "Blue Archive")
                self.assertEqual(indexed.purity, "sfw")
                self.assertEqual(
                    {tag.name for tag in indexed.tags},
                    {"Blue Archive", "manual search"},
                )
                self.assertEqual(
                    {
                        (tag.name, tag.provenance)
                        for tag in indexed.tags
                    },
                    {
                        ("Blue Archive", "wallhaven-api"),
                        ("manual search", "staging-directory"),
                    },
                )
            finally:
                conn.close()


class WallhavenLedgerTests(unittest.TestCase):
    @staticmethod
    def _create_legacy_db(path: Path) -> None:
        legacy = sqlite3.connect(path)
        legacy.executescript(
            """
            CREATE TABLE images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                source TEXT NOT NULL,
                source_site_id TEXT,
                franchise TEXT,
                purity TEXT,
                enrichment_status TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            );
            CREATE TABLE tags (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                category_id INTEGER,
                category TEXT,
                slug TEXT,
                tag_type TEXT,
                provenance TEXT
            );
            CREATE TABLE image_tags (
                image_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                provenance TEXT,
                PRIMARY KEY (image_id, tag_id)
            );
            INSERT INTO images(
                path, filename, source, source_site_id, franchise, purity,
                enrichment_status, indexed_at
            ) VALUES
                ('C:/old/a.jpg', 'a.jpg', 'wallhaven', 'G7XD1D', NULL, NULL,
                 'pending', 'now'),
                ('C:/old/b.jpg', 'b.jpg', 'wallhaven', 'g7xd1d',
                 'Blue Archive', 'sfw', 'ok', 'now');
            INSERT INTO tags(
                id, name, category_id, category, slug, tag_type, provenance
            ) VALUES
                (10, 'Blue Archive', 1, 'Games', 'blue-archive', 'Games',
                 'wallhaven-api'),
                (11, 'Sky', 2, 'General', 'sky', 'General',
                 'wallhaven-api'),
                (12, 'manual search', NULL, NULL, 'manual-search', 'search',
                 'queue-search');
            INSERT INTO image_tags(image_id, tag_id, provenance) VALUES
                (1, 11, 'wallhaven-api'),
                (2, 10, 'wallhaven-api'),
                (2, 12, 'queue-search');
            """
        )
        legacy.commit()
        legacy.close()

    def test_cli_export_is_read_only_deterministic_and_merges_duplicates(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "legacy.sqlite"
            first = root / "first.jsonl"
            second = root / "second.jsonl"
            self._create_legacy_db(db)
            before = db.read_bytes()
            before_hash = hashlib.sha256(before).hexdigest()
            before_mtime = db.stat().st_mtime_ns

            self.assertEqual(
                idx.main([
                    "--db-path", str(db),
                    "--export-wallhaven-ledger", str(first),
                ]),
                0,
            )
            self.assertEqual(
                idx.main([
                    "--db-path", str(db),
                    "--export-wallhaven-ledger", str(second),
                ]),
                0,
            )

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before_hash)
            self.assertEqual(db.stat().st_mtime_ns, before_mtime)
            ro = sqlite3.connect(db)
            try:
                tables = {
                    row[0] for row in ro.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertNotIn("schema_metadata", tables)
            finally:
                ro.close()

            records = [
                json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["schema_version"], 1)
            self.assertEqual(record["record_type"], "wallhaven-enrichment")
            self.assertEqual(record["source"], "wallhaven")
            self.assertEqual(record["source_id"], "g7xd1d")
            self.assertEqual(record["enrichment_status"], "ok")
            self.assertEqual(record["franchise"], "Blue Archive")
            self.assertEqual(record["purity"], "sfw")
            self.assertEqual(record["provenance"], "wallhaven-api")
            self.assertEqual(
                [(tag["name"], tag["type"]) for tag in record["tags"]],
                [("Blue Archive", "Games"), ("Sky", "General")],
            )
            self.assertTrue(
                all(tag["provenance"] == "wallhaven-api" for tag in record["tags"])
            )

    def test_fresh_rebuild_restores_ledger_and_sidecar_tags(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            image_dir = root / "4K" / "landscape" / "wallhaven"
            image_dir.mkdir(parents=True)
            image = image_dir / (
                "src=wallhaven__id=g7xd1d__size=3840x2160__slug=blue-archive.jpg"
            )
            image.write_bytes(b"wallhaven ledger payload")
            _write_metadata_sidecar(
                image,
                tags=[{
                    "name": "manual blue search", "slug": "manual-blue-search",
                    "type": "search", "provenance": "queue-search",
                }],
            )
            ledger = idx.default_wallhaven_ledger_path(root)
            ledger.parent.mkdir(parents=True)
            pending = _wallhaven_ledger_record(
                "g7xd1d", status="pending", franchise=None, purity=None,
            )
            complete = _wallhaven_ledger_record(
                "G7XD1D",
                tags=[{
                    "name": "Blue Archive", "slug": "blue-archive",
                    "type": "Games", "category_id": 1, "category": "Games",
                    "provenance": "wallhaven-api",
                }],
            )
            ledger.write_text(
                json.dumps(pending) + "\n" + json.dumps(complete) + "\n",
                encoding="utf-8",
            )

            conn = idx.connect(Path(tmp) / "fresh.sqlite")
            try:
                stats = idx.ingest_library(conn, root)
                self.assertEqual(stats["ledger_lines"], 2)
                self.assertEqual(stats["ledger_records"], 1)
                self.assertEqual(stats["ledger_superseded"], 1)
                self.assertEqual(stats["ledger_applied"], 1)
                self.assertEqual(stats["ledger_unmatched"], 0)
                row = idx.query(conn, source="wallhaven")[0]
                self.assertEqual(row.enrichment_status, idx.STATUS_OK)
                self.assertEqual(row.franchise, "Blue Archive")
                self.assertEqual(row.purity, "sfw")
                self.assertEqual(
                    {(tag.name, tag.provenance) for tag in row.tags},
                    {
                        ("Blue Archive", "wallhaven-api"),
                        ("manual blue search", "queue-search"),
                    },
                )
            finally:
                conn.close()

    def test_malformed_truncated_line_is_diagnosed_and_ignored(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "wallhaven.jsonl"
            valid = _wallhaven_ledger_record("g7xd1d")
            ledger.write_bytes(
                (json.dumps(valid) + "\n").encode("utf-8")
                + b'{"schema_version":1,"record_type":'
            )
            with self.assertLogs(idx.logger, level="WARNING") as captured:
                records, stats = idx.load_wallhaven_ledger(ledger)
            self.assertEqual(set(records), {"g7xd1d"})
            self.assertEqual(stats["ledger_invalid"], 1)
            self.assertIn(":2:", "\n".join(captured.output))

    def test_enrichment_appends_durable_record_before_fresh_rebuild(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            image_dir = root / "4K" / "landscape" / "wallhaven"
            image_dir.mkdir(parents=True)
            image = image_dir / (
                "src=wallhaven__id=g7xd1d__size=3840x2160__slug=blue-archive.jpg"
            )
            image.write_bytes(b"future enrichment payload")
            _write_metadata_sidecar(
                image,
                tags=[{
                    "name": "manual", "slug": "manual", "type": "search",
                    "provenance": "sidecar",
                }],
            )
            ledger = idx.default_wallhaven_ledger_path(root)
            first_db = idx.connect(Path(tmp) / "first.sqlite")
            try:
                idx.ingest_library(first_db, root)
                response = {
                    "data": {
                        "dimension_x": 3840,
                        "dimension_y": 2160,
                        "purity": "sfw",
                        "tags": [{
                            "name": "Blue Archive", "slug": "blue-archive",
                            "category_id": 1, "category": "Games",
                        }],
                    }
                }
                with mock.patch.object(
                    idx, "_wallhaven_get",
                    return_value=json.dumps(response).encode("utf-8"),
                ):
                    stats = idx.enrich_wallhaven(
                        first_db, None, max_fetch=1, sleep_seconds=0,
                        ledger_path=ledger,
                    )
                self.assertEqual(stats["fetched"], 1)
            finally:
                first_db.close()

            records, ledger_stats = idx.load_wallhaven_ledger(ledger)
            self.assertEqual(ledger_stats["ledger_records"], 1)
            self.assertEqual(records["g7xd1d"]["enrichment_status"], "ok")

            rebuilt = idx.connect(Path(tmp) / "rebuilt.sqlite")
            try:
                ingest_stats = idx.ingest_library(rebuilt, root)
                self.assertEqual(ingest_stats["ledger_applied"], 1)
                row = idx.query(rebuilt, source="wallhaven")[0]
                self.assertEqual(row.enrichment_status, idx.STATUS_OK)
                self.assertEqual(row.franchise, "Blue Archive")
                self.assertEqual(
                    {tag.name for tag in row.tags}, {"Blue Archive", "manual"}
                )
            finally:
                rebuilt.close()

    def test_unauthorized_enrichment_persists_skipped_status(self) -> None:
        import tempfile
        import urllib.error

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            image_dir = root / "4K" / "landscape" / "wallhaven"
            image_dir.mkdir(parents=True)
            image = image_dir / (
                "src=wallhaven__id=g7xd1d__size=3840x2160__slug=private.jpg"
            )
            image.write_bytes(b"private wallhaven payload")
            _write_metadata_sidecar(image)
            ledger = idx.default_wallhaven_ledger_path(root)
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                unauthorized = urllib.error.HTTPError(
                    "https://example.test", 401, "unauthorized", {}, None,
                )
                with mock.patch.object(
                    idx, "_wallhaven_get", side_effect=unauthorized,
                ):
                    stats = idx.enrich_wallhaven(
                        conn, None, max_fetch=1, sleep_seconds=0,
                        ledger_path=ledger,
                    )
                self.assertEqual(stats["skipped"], 1)
            finally:
                conn.close()

            records, _ = idx.load_wallhaven_ledger(ledger)
            self.assertEqual(records["g7xd1d"]["enrichment_status"], "skipped")
            rebuilt = idx.connect(Path(tmp) / "rebuilt.sqlite")
            try:
                idx.ingest_library(rebuilt, root)
                row = idx.query(rebuilt, source="wallhaven")[0]
                self.assertEqual(row.enrichment_status, idx.STATUS_SKIPPED)
            finally:
                rebuilt.close()


class WallhavenTagCoexistenceTests(unittest.TestCase):
    def test_api_and_sidecar_tags_survive_reingest(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "library"
            image_dir = root / "4K" / "landscape" / "wallhaven"
            image_dir.mkdir(parents=True)
            image = image_dir / (
                "src=wallhaven__id=g7xd1d__size=3840x2160__slug=blue-archive.jpg"
            )
            image.write_bytes(b"wallhaven payload")
            _write_metadata_sidecar(
                image,
                tags=[
                    {
                        "name": "manual blue search", "slug": "manual-blue-search",
                        "type": "search", "provenance": "queue-search",
                    }
                ],
            )
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                row = conn.execute("SELECT id, enrichment_status FROM images").fetchone()
                self.assertEqual(row["enrichment_status"], idx.STATUS_PENDING)
                idx._apply_wallhaven_data(
                    conn,
                    row["id"],
                    {
                        "dimension_x": 3840, "dimension_y": 2160, "purity": "sfw",
                        "tags": [
                            {
                                "id": 123, "name": "Blue Archive",
                                "category_id": 1, "category": "Games",
                            }
                        ],
                    },
                )
                conn.commit()
                api_id = conn.execute(
                    "SELECT id FROM tags WHERE name='Blue Archive'"
                ).fetchone()["id"]
                idx.ingest_library(conn, root)
                indexed = idx.query(conn, source="wallhaven")[0]
                self.assertEqual(indexed.enrichment_status, idx.STATUS_OK)
                self.assertEqual(indexed.franchise, "Blue Archive")
                self.assertEqual(indexed.purity, "sfw")
                self.assertEqual(
                    {tag.name for tag in indexed.tags},
                    {"Blue Archive", "manual blue search"},
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT id FROM tags WHERE name='Blue Archive'"
                    ).fetchone()["id"],
                    api_id,
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT provenance FROM tags WHERE name='Blue Archive'"
                    ).fetchone()["provenance"],
                    "wallhaven-api",
                )
            finally:
                conn.close()


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


class QueryTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        root = tmp / "images"
        (root / "4K").mkdir(parents=True)
        (root / "1080p").mkdir(parents=True)
        (root / "4K" / "wallhaven_aaa1_2160x3840.jpg").write_bytes(b"x")
        (root / "4K" / "wallhaven_bbb2_3840x2160.jpg").write_bytes(b"x")
        (root / "1080p" / "922947-1800x2546-virtual+youtuber-mr.lime.jpg").write_bytes(b"x")
        self.conn = idx.connect(tmp / "index.sqlite")
        idx.ingest_library(self.conn, root)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_filter_by_orientation(self) -> None:
        portraits = idx.query(self.conn, orientation="portrait")
        self.assertEqual(len(portraits), 2)
        landscapes = idx.query(self.conn, orientation="landscape")
        self.assertEqual(len(landscapes), 1)

    def test_filter_by_source(self) -> None:
        wh = idx.query(self.conn, source="wallhaven")
        self.assertEqual(len(wh), 2)
        ap = idx.query(self.conn, source="anime_pictures")
        self.assertEqual(len(ap), 1)

    def test_filter_by_franchise(self) -> None:
        rows = idx.query(self.conn, franchise="virtual youtuber")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, "anime-pictures")

    def test_filter_by_tag(self) -> None:
        rows = idx.query(self.conn, tag="mr.lime")
        self.assertEqual(len(rows), 1)

    def test_query_arg_preserves_quoted_spaces(self) -> None:
        parsed = idx._parse_query_arg(
            'franchise="virtual youtuber" tag="long hair" limit=25'
        )
        self.assertEqual(parsed["franchise"], "virtual youtuber")
        self.assertEqual(parsed["tag"], "long hair")
        self.assertEqual(parsed["limit"], "25")

    def test_query_filters_and_validates_nsfw_subcategory(self) -> None:
        image_id = int(
            self.conn.execute("SELECT id FROM images ORDER BY id LIMIT 1").fetchone()[0]
        )
        self.conn.execute(
            "UPDATE images SET content_rating='nsfw', "
            "nsfw_subcategory='explicit' WHERE id=?",
            (image_id,),
        )
        rows = idx.query(
            self.conn,
            content_rating="NSFW",
            nsfw_subcategory="EXPLICIT",
        )
        self.assertEqual([row.id for row in rows], [image_id])
        with self.assertRaisesRegex(ValueError, "requires content_rating"):
            idx.query(self.conn, nsfw_subcategory="explicit")
        with self.assertRaisesRegex(ValueError, "requires content_rating"):
            idx.query(
                self.conn,
                content_rating="sfw",
                nsfw_subcategory="explicit",
            )
        with self.assertRaisesRegex(ValueError, "nsfw_subcategory"):
            idx.query(
                self.conn,
                content_rating="nsfw",
                nsfw_subcategory="other",
            )

    def test_cli_forwards_nsfw_subcategory(self) -> None:
        db_path = Path(self._tmp.name) / "index.sqlite"
        with mock.patch.object(idx, "query", return_value=[]) as query:
            result = idx.main(
                [
                    "--db-path",
                    str(db_path),
                    "--query",
                    "rating=nsfw nsfw_subcategory=fetish",
                ]
            )
        self.assertEqual(result, 0)
        self.assertEqual(query.call_args.kwargs["content_rating"], "nsfw")
        self.assertEqual(query.call_args.kwargs["nsfw_subcategory"], "fetish")


# ---------------------------------------------------------------------------
# Orientation reorg plan (Phase 3 — read-only manifest emit)
# ---------------------------------------------------------------------------


class SchemaV4ContractTests(unittest.TestCase):
    def test_true_schema2_migration_backfills_and_preserves_evidence(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v2.sqlite"
            _create_schema2_fixture(db)
            conn = idx.connect(db)
            try:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertTrue(
                    {
                        "content_rating", "rating_confidence", "rating_basis",
                        "rating_reasons_json", "nsfw_subcategory", "tag_count",
                    }.issubset(idx._table_columns(conn, "images"))
                )
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertTrue({"library_facets", "tag_suggestions"}.issubset(tables))
                row = conn.execute("SELECT * FROM images").fetchone()
                self.assertEqual(
                    (
                        row["content_rating"], row["nsfw_subcategory"],
                        row["tag_count"],
                    ),
                    ("nsfw", "explicit", 1),
                )
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute(
                        "SELECT last_processed_source_site_id FROM enrichment_progress"
                    ).fetchone()[0],
                    "zz9",
                )
                self.assertGreater(
                    conn.execute("SELECT COUNT(*) FROM library_facets").fetchone()[0], 0,
                )
            finally:
                conn.close()
            reopened = idx.connect(db)
            try:
                self.assertEqual(reopened.execute("SELECT COUNT(*) FROM tags").fetchone()[0], 1)
                self.assertEqual(reopened.execute("SELECT tag_count FROM images").fetchone()[0], 1)
            finally:
                reopened.close()

    def test_schema2_migration_rolls_back_before_version_publication(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v2.sqlite"
            _create_schema2_fixture(db)
            with mock.patch.object(
                idx, "refresh_derived_metadata", side_effect=RuntimeError("injected"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    idx.connect(db)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM schema_metadata WHERE key='schema_version'"
                    ).fetchone()[0],
                    "2",
                )
                self.assertNotIn("content_rating", {row[1] for row in conn.execute("PRAGMA table_info(images)")})
                tables = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertNotIn("library_facets", tables)
                self.assertNotIn("tag_suggestions", tables)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM enrichment_progress").fetchone()[0], 1,
                )
            finally:
                conn.close()

    def test_materialized_ratings_facets_and_reopen_are_idempotent(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite"
            conn = idx.connect(db)
            image_id = _insert_minimal_image(conn, 1)
            idx._replace_image_tags(
                conn, image_id, "zerochan",
                [{"name": "hentai", "type": "rating", "provenance": "fixture"}],
            )
            result = idx.refresh_derived_metadata(conn)
            conn.commit()
            row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
            self.assertEqual(result.images, 1)
            self.assertEqual(row["content_rating"], "nsfw")
            self.assertEqual(row["nsfw_subcategory"], "explicit")
            self.assertEqual(row["rating_basis"], "explicit-tag")
            self.assertEqual(json.loads(row["rating_reasons_json"]), ["hentai"])
            self.assertEqual(row["tag_count"], 1)
            self.assertIn("content_rating", idx.read_library_facets(conn))
            conn.close()

            reopened = idx.connect(db)
            try:
                self.assertEqual(reopened.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(
                    reopened.execute("SELECT COUNT(*) FROM tags").fetchone()[0], 1,
                )
                self.assertEqual(
                    reopened.execute(
                        "SELECT value FROM schema_metadata WHERE key='schema_version'"
                    ).fetchone()[0],
                    "4",
                )
            finally:
                reopened.close()

    def test_schema_marker_mismatch_and_future_version_fail_before_write(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            for name, user_version, metadata_version in (
                ("mismatch", 0, 2), ("future", 5, 5),
            ):
                db = Path(tmp) / f"{name}.sqlite"
                conn = sqlite3.connect(db)
                conn.execute("CREATE TABLE schema_metadata(key TEXT PRIMARY KEY,value TEXT)")
                conn.execute(
                    "INSERT INTO schema_metadata VALUES ('schema_version',?)",
                    (str(metadata_version),),
                )
                conn.execute(f"PRAGMA user_version={user_version}")
                conn.commit()
                conn.close()
                before = db.read_bytes()
                with self.assertRaisesRegex(ValueError, "schema"):
                    idx.connect(db)
                self.assertEqual(db.read_bytes(), before)

    def test_new_schema_enforces_subcategory_domain_invariant_and_index(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                self.assertIn(
                    "nsfw_subcategory", idx._table_columns(conn, "images")
                )
                index_columns = [
                    row[2]
                    for row in conn.execute(
                        "PRAGMA index_info(idx_images_nsfw_subcategory)"
                    )
                ]
                self.assertEqual(
                    index_columns, ["content_rating", "nsfw_subcategory", "id"]
                )
                image_id = _insert_minimal_image(conn, 1, purity="sfw")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE images SET nsfw_subcategory='explicit' WHERE id=?",
                        (image_id,),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "UPDATE images SET content_rating='nsfw', "
                        "nsfw_subcategory='invalid' WHERE id=?",
                        (image_id,),
                    )
            finally:
                conn.close()

    def test_schema3_migration_backfills_subcategory_and_publishes_last(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v3.sqlite"
            _create_schema3_fixture(db)
            conn = idx.connect(db)
            try:
                row = conn.execute("SELECT * FROM images").fetchone()
                self.assertEqual(
                    (row["content_rating"], row["nsfw_subcategory"]),
                    ("nsfw", "explicit"),
                )
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM schema_metadata "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "4",
                )
                self.assertIn(
                    "idx_images_nsfw_subcategory",
                    {
                        row[1]
                        for row in conn.execute("PRAGMA index_list(images)")
                    },
                )
                trigger_names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
                self.assertTrue(
                    {
                        "trg_images_nsfw_subcategory_insert",
                        "trg_images_nsfw_subcategory_update",
                    }.issubset(trigger_names)
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "non-NSFW images require"
                ):
                    conn.execute(
                        "UPDATE images SET content_rating='sfw', "
                        "nsfw_subcategory='explicit' WHERE id=?",
                        (row["id"],),
                    )
            finally:
                conn.close()

    def test_schema3_migration_rolls_back_subcategory_and_version(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v3.sqlite"
            _create_schema3_fixture(db)
            with mock.patch.object(
                idx, "refresh_derived_metadata", side_effect=RuntimeError("injected")
            ):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    idx.connect(db)
            conn = sqlite3.connect(db)
            try:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM schema_metadata "
                        "WHERE key='schema_version'"
                    ).fetchone()[0],
                    "3",
                )
                self.assertNotIn(
                    "nsfw_subcategory",
                    {row[1] for row in conn.execute("PRAGMA table_info(images)")},
                )
                self.assertNotIn(
                    "idx_images_nsfw_subcategory",
                    {row[1] for row in conn.execute("PRAGMA index_list(images)")},
                )
                self.assertFalse(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                        "AND name LIKE 'trg_images_nsfw_subcategory_%'"
                    ).fetchall()
                )
            finally:
                conn.close()

    def test_refresh_uses_authoritative_tags_and_nsfw_only_facets(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conn = idx.connect(Path(tmp) / "index.sqlite")
            cases = (
                (1, "nude", "nudity"),
                (2, "bondage", "fetish"),
                (3, "oral sex", "explicit"),
            )
            for number, tag, _expected in cases:
                image_id = _insert_minimal_image(conn, number, purity="nsfw")
                idx._replace_image_tags(
                    conn,
                    image_id,
                    "zerochan",
                    [{"name": tag, "type": "rating", "provenance": "fixture"}],
                )
            purity_only_id = _insert_minimal_image(conn, 4, purity="nsfw")
            suggestion_only_id = _insert_minimal_image(conn, 5)
            idx.upsert_tag_suggestion(
                conn,
                image_id=suggestion_only_id,
                label="oral sex",
                confidence=0.99,
                generator="fixture",
                model_version="1",
                provenance="synthetic",
            )
            idx.refresh_derived_metadata(conn)
            rows = {
                int(row["id"]): (row["content_rating"], row["nsfw_subcategory"])
                for row in conn.execute(
                    "SELECT id,content_rating,nsfw_subcategory FROM images"
                )
            }
            self.assertEqual(
                [rows[number][1] for number in sorted(rows)[:3]],
                [expected for _number, _tag, expected in cases],
            )
            self.assertEqual(rows[purity_only_id], ("nsfw", "unspecified"))
            self.assertEqual(rows[suggestion_only_id], ("unknown", "unspecified"))
            facet = {
                item.value: item.count
                for item in idx.read_library_facets(conn)["nsfw_subcategory"]
            }
            self.assertEqual(
                facet,
                {"nudity": 1, "fetish": 1, "explicit": 1, "unspecified": 1},
            )
            conn.close()

    def test_verifier_detects_subcategory_field_and_facet_drift(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root, db, _image, _sidecar = _create_verified_fixture(Path(tmp))
            conn = sqlite3.connect(db)
            try:
                conn.execute(
                    "UPDATE images SET content_rating='nsfw', "
                    "nsfw_subcategory='fetish'"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO library_facets "
                    "VALUES ('nsfw_subcategory','fetish',1,'drift')"
                )
                conn.commit()
            finally:
                conn.close()
            report = idx.verify_library(db, root)
            self.assertGreater(report["counts"]["derived_metadata_failures"], 0)
            self.assertGreater(report["counts"]["facet_failures"], 0)
            self.assertIn("nsfw_subcategory", json.dumps(report["issues"]))


class DiscoveryQueryContractTests(unittest.TestCase):
    def test_indexed_plans_cover_newest_size_desc_and_sha_lookup(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conn = idx.connect(Path(tmp) / "index.sqlite")
            for number in range(1, 6):
                image_id = _insert_minimal_image(conn, number)
                conn.execute(
                    "UPDATE images SET size_bytes=?, sha256=? WHERE id=?",
                    (number * 100, f"{number:064x}", image_id),
                )

            def plan(sql: str, params: tuple[object, ...] = ()) -> list[str]:
                return [
                    str(row["detail"])
                    for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
                ]

            for sort, index_name in (
                ("newest", "idx_images_newest"),
                ("size_desc", "idx_images_size_desc"),
            ):
                with self.subTest(sort=sort):
                    details = plan(
                        "SELECT images.* FROM images ORDER BY "
                        + idx._QUERY_ORDER_BY[sort]
                        + " LIMIT ? OFFSET ?",
                        (10, 0),
                    )
                    self.assertTrue(
                        any(f"USING INDEX {index_name}" in detail for detail in details),
                        details,
                    )
                    self.assertFalse(
                        any("USE TEMP B-TREE FOR ORDER BY" in detail for detail in details),
                        details,
                    )

            sha_details = plan(
                "SELECT path, ext FROM images WHERE lower(sha256) = ? LIMIT 2",
                (f"{1:064x}",),
            )
            self.assertTrue(
                any(
                    "SEARCH images USING INDEX idx_images_sha256_lower" in detail
                    for detail in sha_details
                ),
                sha_details,
            )
            self.assertFalse(
                any(detail == "SCAN images" for detail in sha_details), sha_details,
            )
            conn.close()

    def test_constant_shape_hydration_sorts_shuffle_and_literal_autocomplete(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conn = idx.connect(Path(tmp) / "index.sqlite")
            image_ids: list[int] = []
            for number in range(1, 13):
                image_id = _insert_minimal_image(conn, number)
                image_ids.append(image_id)
                tags = [
                    {
                        "name": f"tag-{tag_number}", "type": "topic",
                        "provenance": "fixture",
                    }
                    for tag_number in range(number % 4)
                ]
                if number in {1, 2}:
                    tags.append(
                        {
                            "name": "100% literal", "type": "topic",
                            "provenance": "fixture",
                        }
                    )
                idx._replace_image_tags(conn, image_id, "zerochan", tags)
            idx.refresh_derived_metadata(conn)
            conn.commit()

            def traced_count(limit: int) -> int:
                statements: list[str] = []
                conn.set_trace_callback(statements.append)
                try:
                    idx.query(conn, limit=limit)
                finally:
                    conn.set_trace_callback(None)
                return sum(
                    statement.lstrip().upper().startswith("SELECT")
                    for statement in statements
                )

            self.assertEqual(traced_count(1), traced_count(12))
            least = idx.query(conn, sort="least_tagged", limit=12)
            self.assertEqual(
                [row.tag_count for row in least],
                sorted(row.tag_count for row in least),
            )
            first = idx.query(conn, sort="shuffle", shuffle_seed=42, limit=6)
            second = idx.query(
                conn, sort="shuffle", shuffle_seed=42, limit=6, offset=6,
            )
            repeated = idx.query(conn, sort="shuffle", shuffle_seed=42, limit=6)
            self.assertEqual([row.id for row in first], [row.id for row in repeated])
            self.assertEqual(len({row.id for row in first + second}), 12)
            with self.assertRaisesRegex(ValueError, "shuffle_seed"):
                idx.query(conn, sort="shuffle")
            matches = idx.counted_tag_autocomplete(conn, "100%", 10)
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].image_count, 2)
            conn.close()


class SuggestionBoundaryTests(unittest.TestCase):
    def test_reviewed_suggestions_are_immutable_to_later_upserts(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conn = idx.connect(Path(tmp) / "index.sqlite")
            image_id = _insert_minimal_image(conn, 1)
            for number, review_status in enumerate(("accepted", "rejected"), 1):
                with self.subTest(review_status=review_status):
                    pending = idx.upsert_tag_suggestion(
                        conn, image_id=image_id, label=f"Visual Label {number}",
                        confidence=0.6, generator="visual-test",
                        model_version=f"v{number}", provenance="first-run",
                    )
                    reviewed = idx.review_tag_suggestion(
                        conn, pending.id, review_status=review_status,
                        reviewer="tester", decision_note="human decision",
                    )
                    rerun = idx.upsert_tag_suggestion(
                        conn, image_id=image_id, label=f"VISUAL LABEL {number}",
                        confidence=0.99, generator="visual-test",
                        model_version=f"v{number}", provenance="later-run",
                    )
                    self.assertEqual(rerun, reviewed)

            pending = idx.upsert_tag_suggestion(
                conn, image_id=image_id, label="Refreshable", confidence=0.5,
                generator="visual-test", model_version="pending",
                provenance="first-run",
            )
            refreshed = idx.upsert_tag_suggestion(
                conn, image_id=image_id, label="REFRESHABLE", confidence=0.75,
                generator="visual-test", model_version="pending",
                provenance="later-run",
            )
            self.assertEqual(refreshed.id, pending.id)
            self.assertEqual(refreshed.review_status, "pending")
            self.assertEqual(refreshed.label, "REFRESHABLE")
            self.assertEqual(refreshed.confidence, 0.75)
            self.assertEqual(refreshed.provenance, "later-run")
            conn.close()

    def test_suggestions_never_become_tags_ratings_or_autocomplete(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conn = idx.connect(Path(tmp) / "index.sqlite")
            image_id = _insert_minimal_image(conn, 1)
            suggestion = idx.upsert_tag_suggestion(
                conn, image_id=image_id, label="hentai", confidence=0.97,
                generator="visual-test", model_version="v1",
                provenance="synthetic-model",
            )
            idx.refresh_derived_metadata(conn)
            accepted = idx.review_tag_suggestion(
                conn, suggestion.id, review_status="accepted", reviewer="tester",
            )
            preserved = idx.upsert_tag_suggestion(
                conn, image_id=image_id, label="HENTAI", confidence=0.99,
                generator="visual-test", model_version="v1",
                provenance="synthetic-model-rerun",
            )
            idx.refresh_derived_metadata(conn)
            row = idx.query(conn)[0]
            self.assertEqual(accepted.review_status, "accepted")
            self.assertEqual(preserved, accepted)
            self.assertEqual(row.content_rating.rating, "unknown")
            self.assertEqual(row.tag_count, 0)
            self.assertEqual(row.tags, ())
            self.assertEqual(len(row.tag_suggestions), 1)
            self.assertEqual(idx.counted_tag_autocomplete(conn, "hentai"), [])
            with self.assertRaisesRegex(ValueError, "invalid suggestion transition"):
                idx.review_tag_suggestion(
                    conn, suggestion.id, review_status="rejected", reviewer="other",
                )

            pending = idx.upsert_tag_suggestion(
                conn, image_id=image_id, label="adult content", confidence=0.8,
                generator="visual-test", model_version="v2",
                provenance="synthetic-model",
            )
            conn.execute(
                "CREATE TRIGGER ignore_test_review BEFORE UPDATE OF review_status "
                "ON tag_suggestions WHEN OLD.id=%d BEGIN SELECT RAISE(IGNORE); END"
                % pending.id
            )
            with self.assertRaisesRegex(ValueError, "concurrent decision"):
                idx.review_tag_suggestion(
                    conn, pending.id, review_status="accepted", reviewer="tester",
                )
            conn.close()


class GenericProviderLedgerTests(unittest.TestCase):
    @staticmethod
    def record(source_id: str, tag_name: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "record_type": "provider-enrichment",
            "source": "zerochan",
            "source_id": source_id,
            "status": "ok",
            "tags": [{"name": tag_name, "type": "topic"}],
            "provenance": "zerochan-captured-v1",
            "captured_at": "2026-07-20T12:00:00+00:00",
            "error": None,
        }

    def test_strict_durable_import_replaces_only_exact_provenance(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "provider.jsonl"
            idx.append_provider_ledger_record(ledger, self.record("AbC", "hentai"))
            idx.append_provider_ledger_record(ledger, self.record("abc", "landscape"))
            records, load_stats = idx.load_provider_ledger(ledger)
            self.assertEqual(load_stats["provider_ledger_superseded"], 1)
            with self.assertRaisesRegex(ValueError, "type"):
                invalid = self.record("x", "bad")
                invalid["tags"] = [{"name": "untyped"}]
                idx.normalize_provider_ledger_record(invalid)

            conn = idx.connect(Path(tmp) / "index.sqlite")
            image_id = _insert_minimal_image(
                conn, 1, source="zerochan", source_id="ABC",
            )
            idx._replace_image_tags(
                conn, image_id, "zerochan",
                [{"name": "search context", "type": "search", "provenance": "sidecar"}],
            )
            stats = idx.apply_provider_enrichment_records(conn, records)
            row = idx.query(conn)[0]
            self.assertEqual(stats["provider_records_matched"], 1)
            self.assertEqual(
                {(tag.name, tag.provenance) for tag in row.tags},
                {
                    ("search context", "sidecar"),
                    ("landscape", "zerochan-captured-v1"),
                },
            )
            self.assertEqual(row.tag_count, 2)
            coverage = idx.provider_coverage(conn)
            self.assertEqual(coverage["total_images"], 1)
            self.assertTrue(coverage["by_provenance"])
            conn.close()


class WallhavenResumeV3Tests(unittest.TestCase):
    @staticmethod
    def response(source_id: str) -> bytes:
        return json.dumps(
            {
                "data": {
                    "id": source_id, "purity": "sfw",
                    "dimension_x": 1920, "dimension_y": 1080, "tags": [],
                }
            }
        ).encode("utf-8")

    def test_pending_queue_ignores_old_cursor_and_orders_mixed_case(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            conn = idx.connect(Path(tmp) / "index.sqlite")
            for number, source_id in enumerate(("zz9", "AA1", "mm2", "bb3"), 1):
                _insert_minimal_image(
                    conn, number, source="wallhaven", source_id=source_id,
                )
            conn.execute(
                "INSERT INTO enrichment_progress VALUES ('wallhaven','mm9','now')"
            )
            conn.commit()
            seen: list[str] = []

            def fetch(url: str) -> bytes:
                source_id = url.split("/w/", 1)[1].split("?", 1)[0]
                seen.append(source_id)
                return self.response(source_id)

            ledger = Path(tmp) / "wallhaven.jsonl"
            with mock.patch.object(idx, "_wallhaven_get", side_effect=fetch):
                stats = idx.enrich_wallhaven(
                    conn, None, max_attempts=4, sleep_seconds=0,
                    ledger_path=ledger,
                )
            self.assertEqual(seen, ["AA1", "bb3", "mm2", "zz9"])
            self.assertEqual(stats["attempted"], 4)
            self.assertEqual(stats["remaining"], 0)
            records, ledger_stats = idx.load_wallhaven_ledger(ledger)
            self.assertEqual(len(records), 4)
            self.assertEqual(ledger_stats["ledger_lines"], 8)
            conn.close()

    def test_terminal_ledger_rebuilds_after_interrupt_before_sqlite(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = idx.connect(tmp_path / "first.sqlite")
            _insert_minimal_image(
                conn, 1, source="wallhaven", source_id="g7xd1d",
            )
            conn.commit()
            ledger = tmp_path / "wallhaven.jsonl"
            with mock.patch.object(
                idx, "_wallhaven_get", return_value=self.response("g7xd1d"),
            ), mock.patch.object(
                idx, "_apply_wallhaven_data", side_effect=KeyboardInterrupt,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    idx.enrich_wallhaven(
                        conn, None, max_attempts=1, sleep_seconds=0,
                        ledger_path=ledger,
                    )
            self.assertEqual(
                conn.execute("SELECT enrichment_status FROM images").fetchone()[0],
                "pending",
            )
            records, _ = idx.load_wallhaven_ledger(ledger)
            self.assertEqual(records["g7xd1d"]["enrichment_status"], "ok")
            conn.close()

            root = tmp_path / "library"
            _write_canonical_fixture_image(root, source_id="g7xd1d")
            rebuilt = idx.connect(tmp_path / "rebuilt.sqlite")
            try:
                idx.ingest_library(
                    rebuilt, root, ledger_path=ledger,
                    provider_ledger_path=tmp_path / "provider.jsonl",
                )
                self.assertEqual(idx.query(rebuilt)[0].enrichment_status, "ok")
                with mock.patch.object(idx, "_wallhaven_get") as fetch:
                    resumed = idx.enrich_wallhaven(
                        rebuilt, None, max_attempts=1, sleep_seconds=0,
                        ledger_path=ledger,
                    )
                self.assertEqual(resumed["attempted"], 0)
                fetch.assert_not_called()
            finally:
                rebuilt.close()


class ReorgPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        root = tmp / "images"
        (root / "4K").mkdir(parents=True)
        (root / "1080p").mkdir(parents=True)
        # Portrait + landscape wallhaven in 4K; anime-pictures portrait in 1080p.
        (root / "4K" / "wallhaven_aaa1_2160x3840.jpg").write_bytes(b"x")
        (root / "4K" / "wallhaven_bbb2_3840x2160.jpg").write_bytes(b"x")
        (root / "1080p" / "922947-1800x2546-virtual+youtuber-mr.lime.jpg").write_bytes(b"x")
        self.root = root
        self.conn = idx.connect(tmp / "index.sqlite")
        idx.ingest_library(self.conn, root)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def _row(self, filename: str) -> object:
        return self.conn.execute(
            "SELECT path, source, resolution_bucket, orientation "
            "FROM images WHERE filename = ?", (filename,),
        ).fetchone()

    def test_destination_identifiable_uses_bucket_then_orientation(self) -> None:
        row = self._row("wallhaven_aaa1_2160x3840.jpg")
        dest_dir, orientation = idx.destination_for(row)
        self.assertEqual(dest_dir, "4K/portrait/wallhaven")
        self.assertEqual(orientation, "portrait")

    def test_destination_unknown_source_goes_to_unknown_folder(self) -> None:
        # Synthesize an unknown-source row so we don't depend on the fixture tree.
        # Write a real PNG so the header reader populates orientation.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "images"
            (root / "SD").mkdir(parents=True)
            _write_png(root / "SD" / "3301538.png", 700, 953)
            conn = idx.connect(Path(tmp) / "index.sqlite")
            idx.ingest_library(conn, root)
            try:
                row = conn.execute(
                    "SELECT path, source, resolution_bucket, orientation "
                    "FROM images WHERE filename = '3301538.png'",
                ).fetchone()
                dest_dir, orientation = idx.destination_for(row)
                self.assertEqual(dest_dir, "SD/portrait/unknown")
                self.assertEqual(orientation, "portrait")
            finally:
                conn.close()

    def test_destination_null_orientation_falls_back_to_unknown_label(self) -> None:
        row = self._row("wallhaven_aaa1_2160x3840.jpg")
        # Force a None orientation without touching the file: build a fake Row
        # subclass. sqlite3.Row is read-only, so use a tiny stand-in that
        # supports __getitem__ by column name (what destination_for uses).
        class FakeRow:
            def __getitem__(self, key: str) -> object:
                data = {
                    "source": "wallhaven",
                    "resolution_bucket": "4K",
                    "orientation": None,
                }
                return data[key]
        dest_dir, orientation = idx.destination_for(FakeRow())
        self.assertEqual(dest_dir, "4K/unknown/wallhaven")
        self.assertEqual(orientation, "unknown")

    def test_emit_plan_writes_csv_with_expected_rows(self) -> None:
        import csv
        plan_path = self.root.parent / "plan.csv"
        stats = idx.emit_reorg_plan(self.conn, plan_path, self.root)
        self.assertEqual(stats["emitted"], 3)
        self.assertEqual(stats["missing"], 0)
        with plan_path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        # Every source path is one of the three fixture files.
        source_basenames = {
            Path(r["SourcePath"]).name for r in rows
        }
        self.assertEqual(
            source_basenames,
            {
                "wallhaven_aaa1_2160x3840.jpg",
                "wallhaven_bbb2_3840x2160.jpg",
                "922947-1800x2546-virtual+youtuber-mr.lime.jpg",
            },
        )
        # Verify the destination dirs present (set-based; ORDER BY path).
        dest_dirs = {r["DestinationDir"] for r in rows}
        self.assertIn("4K/portrait/wallhaven", dest_dirs)
        self.assertIn("4K/landscape/wallhaven", dest_dirs)
        self.assertIn("1080p/portrait/anime-pictures", dest_dirs)

    def test_emit_plan_skips_missing_source_files(self) -> None:
        import csv
        # Delete one source file out of band; the plan must skip it, not crash.
        (self.root / "4K" / "wallhaven_aaa1_2160x3840.jpg").unlink()
        plan_path = self.root.parent / "plan2.csv"
        stats = idx.emit_reorg_plan(self.conn, plan_path, self.root)
        self.assertEqual(stats["emitted"], 2)
        self.assertEqual(stats["missing"], 1)

    def test_emit_plan_rejects_indexed_paths_outside_root(self) -> None:
        outside_root = self.root.parent / "other-library"
        (outside_root / "4K").mkdir(parents=True)
        (outside_root / "4K" / "wallhaven_ccc3_3840x2160.jpg").write_bytes(b"x")
        idx.ingest_library(self.conn, outside_root)
        plan_path = self.root.parent / "guarded-plan.csv"
        stats = idx.emit_reorg_plan(self.conn, plan_path, self.root)
        self.assertEqual(stats["emitted"], 3)
        self.assertEqual(stats["outside_root"], 1)


# ---------------------------------------------------------------------------
# .env parsing
# ---------------------------------------------------------------------------


class LoadApiKeyTests(unittest.TestCase):
    def test_reads_key(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False, encoding="utf-8"
        ) as tf:
            tf.write("# comment\nWALLHAVEN_API_KEY=abc123secret\nOTHER=x\n")
            path = Path(tf.name)
        try:
            self.assertEqual(idx.load_api_key(path), "abc123secret")
        finally:
            path.unlink(missing_ok=True)

    def test_strips_quotes(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False, encoding="utf-8"
        ) as tf:
            tf.write('WALLHAVEN_API_KEY="quoted-key"\n')
            path = Path(tf.name)
        try:
            self.assertEqual(idx.load_api_key(path), "quoted-key")
        finally:
            path.unlink(missing_ok=True)

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(idx.load_api_key(Path("__nope__.env")))

    def test_missing_key_returns_none(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False, encoding="utf-8"
        ) as tf:
            tf.write("OTHER=value\n")
            path = Path(tf.name)
        try:
            self.assertIsNone(idx.load_api_key(path))
        finally:
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Surface guard — mirrors test_common_utils.py / test_package_surface.py
# ---------------------------------------------------------------------------


class IndexLibrarySurfaceTests(unittest.TestCase):
    """This module never mutates wallpaper images. It must never grow
    move/sort/delete/finalize helpers (metadata ledger writes are allowed)."""

    def test_no_file_mutation_helpers(self) -> None:
        forbidden = [
            "sort_files",
            "move_file",
            "move_to_bucket",
            "delete_image",
            "reorganize",
            "finalize",
            "apply_sort",
            "rename_image",
        ]
        for name in forbidden:
            self.assertFalse(
                hasattr(idx, name),
                f"index_library must not expose {name}() — "
                "Python is read-only on the library",
            )


# ---------------------------------------------------------------------------
# HTML activity dashboard
# ---------------------------------------------------------------------------


class DashboardTests(unittest.TestCase):
    """The --dashboard generator: read-only HTML emission from mtimes + index."""

    def _make_tree(self, tmp_path):
        """Tiny fake library: 3 images across 2 buckets."""
        root = tmp_path / "images"
        (root / "4K").mkdir(parents=True)
        (root / "1080p").mkdir(parents=True)
        (root / "4K" / "wallhaven_aaa1_2160x3840.jpg").write_bytes(b"x")
        (root / "4K" / "wallhaven_bbb2_3840x2160.jpg").write_bytes(b"x")
        (root / "1080p" / "922947-1800x2546-virtual+youtuber-mr.lime.jpg").write_bytes(b"x")
        return root

    def test_dashboard_writes_valid_html(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                out = Path(tmp) / "dash.html"
                stats = idx.generate_dashboard(conn, out, root)

                self.assertTrue(out.is_file())
                self.assertGreater(out.stat().st_size, 200)
                self.assertEqual(stats["present"], 3)
                self.assertEqual(stats["missing"], 0)
                self.assertEqual(stats["emitted"], 1)

                html_text = out.read_text(encoding="utf-8")
                self.assertIn("<!DOCTYPE html>", html_text)
                self.assertIn("<svg", html_text)
                self.assertIn("Recent daily activity", html_text)
                self.assertIn("Hour of day", html_text)
                self.assertTrue(html_text.rstrip().endswith("</html>"))
            finally:
                conn.close()

    def test_dashboard_counts_match(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                out = Path(tmp) / "dash.html"
                idx.generate_dashboard(conn, out, root)
                html_text = out.read_text(encoding="utf-8")
                # Total images (3) present somewhere in the output.
                self.assertIn("3", html_text)
                # At least one day with activity.
                self.assertGreater(html_text.count("image(s)"), 0)
            finally:
                conn.close()

    def test_dashboard_timeline_is_bounded_and_accessible(self):
        from datetime import date, timedelta

        start = date(2022, 1, 1)
        per_day = {
            (start + timedelta(days=i)).isoformat(): (i % 17) + 1
            for i in range(1200)
        }
        rendered = idx._render_svg_timeline(per_day)

        self.assertIn('width="100%"', rendered)
        self.assertIn('viewBox="0 0 960 250"', rendered)
        self.assertIn('role="img"', rendered)
        self.assertIn('All-time peak:', rendered)
        self.assertLessEqual(rendered.count('<rect class="bar"'), 365)

    def test_dashboard_navigation_targets_and_table_headers_exist(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                out = Path(tmp) / "dash.html"
                idx.generate_dashboard(conn, out, root)
                html_text = out.read_text(encoding="utf-8")
                self.assertIn('href="#resolution"', html_text)
                self.assertIn('id="resolution"', html_text)
                self.assertIn('<th scope="col">Category</th>', html_text)
                self.assertIn('aria-label="Wallpaper saves by local hour"', html_text)
            finally:
                conn.close()

    def test_dashboard_is_read_only_on_library(self):
        """Generating the dashboard must not move/create/delete any image."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            before = {
                str(p.relative_to(root)): p.stat().st_mtime
                for p in root.rglob("*") if p.is_file()
            }
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                idx.generate_dashboard(conn, Path(tmp) / "out" / "dash.html", root)
            finally:
                conn.close()
            after = {
                str(p.relative_to(root)): p.stat().st_mtime
                for p in root.rglob("*") if p.is_file()
            }
            self.assertEqual(before, after)

    def test_dashboard_skips_missing_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_tree(Path(tmp))
            conn = idx.connect(Path(tmp) / "index.sqlite")
            try:
                idx.ingest_library(conn, root)
                (root / "4K" / "wallhaven_aaa1_2160x3840.jpg").unlink()
                out = Path(tmp) / "dash.html"
                stats = idx.generate_dashboard(conn, out, root)
                self.assertEqual(stats["present"], 2)
                self.assertEqual(stats["missing"], 1)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
