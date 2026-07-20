from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from dl_engine import index_library as idx
from dl_engine import library_browser


class LibraryBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.db = self.root / "index.sqlite"

        conn = idx.connect(self.db)
        try:
            rows = [
                ("safe image.jpg", "sfw", "wallhaven"),
                ("adult.jpg", "nsfw", "wallhaven"),
                ("tagged.jpg", None, "anime-pictures"),
                ("unknown.jpg", None, "zerochan"),
                ("review.jpg", "sketchy", "wallhaven"),
            ]
            for filename, purity, source in rows:
                path = self.library / filename
                path.write_bytes(b"image")
                conn.execute(
                    "INSERT INTO images ("
                    "path, filename, source, purity, orientation, "
                    "resolution_bucket, width, height, enrichment_status, indexed_at"
                    ") VALUES (?, ?, ?, ?, 'landscape', '1080p', 1920, 1080, 'ok', 'now')",
                    (str(path), filename, source, purity),
                )
            sort_values = {
                "safe image.jpg": ("2026-01-01T00:00:00Z", 100, 1920, 1080),
                "review.jpg": ("2026-01-02T00:00:00Z", 200, 800, 600),
                "unknown.jpg": ("2026-01-03T00:00:00Z", 300, 2560, 1440),
                "tagged.jpg": ("2026-01-04T00:00:00Z", 400, 3840, 2160),
                "adult.jpg": ("2026-01-05T00:00:00Z", 500, 1280, 720),
            }
            for filename, values in sort_values.items():
                conn.execute(
                    "UPDATE images SET download_recorded_at=?, size_bytes=?, "
                    "width=?, height=? WHERE filename=?",
                    (*values, filename),
                )
            conn.execute(
                "INSERT INTO tags (id, name, source, tag_type, provenance) "
                "VALUES (1, 'nude', 'anime-pictures', 'tag', 'queue-browser-url')"
            )
            tagged_id = conn.execute(
                "SELECT id FROM images WHERE filename='tagged.jpg'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO image_tags (image_id, tag_id, provenance) VALUES (?, 1, ?)",
                (tagged_id, "queue-browser-url"),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_query_page_separates_nsfw_and_encodes_urls(self) -> None:
        page = library_browser.query_library_page(
            self.db,
            self.library,
            {"rating": "nsfw", "limit": "10"},
        )
        self.assertEqual(
            [item["filename"] for item in page["items"]],
            ["adult.jpg", "tagged.jpg"],
        )
        self.assertTrue(all(item["content_rating"] == "nsfw" for item in page["items"]))
        self.assertTrue(all(item["url"].startswith("/library/") for item in page["items"]))

        safe = library_browser.query_library_page(
            self.db,
            self.library,
            {"rating": "sfw", "limit": "10"},
        )
        self.assertEqual(safe["items"][0]["url"], "/library/safe%20image.jpg")

    def test_query_page_supports_offset_and_has_more(self) -> None:
        first = library_browser.query_library_page(
            self.db,
            self.library,
            {"rating": "nsfw", "limit": "1"},
        )
        self.assertTrue(first["page"]["has_more"])
        second = library_browser.query_library_page(
            self.db,
            self.library,
            {"rating": "nsfw", "limit": "1", "offset": "1"},
        )
        self.assertFalse(second["page"]["has_more"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])

    def test_query_page_remains_readable_during_wal_write_transaction(self) -> None:
        writer = sqlite3.connect(self.db)
        try:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute(
                "UPDATE images SET filename='uncommitted.jpg' "
                "WHERE filename='safe image.jpg'"
            )
            page = library_browser.query_library_page(
                self.db,
                self.library,
                {"rating": "sfw", "limit": "10"},
            )
        finally:
            writer.rollback()
            writer.close()

        self.assertEqual(page["items"][0]["filename"], "safe image.jpg")

    def test_query_page_supports_stable_full_collection_sorts(self) -> None:
        newest = library_browser.query_library_page(
            self.db, self.library, {"sort": "newest", "limit": "10"}
        )
        self.assertEqual(
            [item["filename"] for item in newest["items"]],
            [
                "adult.jpg", "tagged.jpg", "unknown.jpg", "review.jpg",
                "safe image.jpg",
            ],
        )
        self.assertEqual(newest["filters"]["sort"], "newest")
        self.assertEqual(newest["items"][0]["downloaded_at"], "2026-01-05T00:00:00Z")

        quality = library_browser.query_library_page(
            self.db, self.library, {"sort": "resolution_desc", "limit": "2"}
        )
        quality_next = library_browser.query_library_page(
            self.db,
            self.library,
            {"sort": "resolution_desc", "limit": "2", "offset": "2"},
        )
        self.assertEqual(
            [item["filename"] for item in quality["items"]],
            ["tagged.jpg", "unknown.jpg"],
        )
        self.assertEqual(
            [item["filename"] for item in quality_next["items"]],
            ["safe image.jpg", "adult.jpg"],
        )

    def test_facets_report_all_rating_buckets(self) -> None:
        facets = library_browser.library_facets(self.db)
        self.assertEqual(
            facets["ratings"],
            {"sfw": 1, "suggestive": 1, "nsfw": 2, "unknown": 1},
        )
        self.assertEqual(facets["indexed_images"], 5)

    def test_invalid_filter_values_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "rating"):
            library_browser.query_library_page(
                self.db, self.library, {"rating": "anything"}
            )
        with self.assertRaisesRegex(ValueError, "between"):
            library_browser.query_library_page(
                self.db, self.library, {"limit": "5000"}
            )
        with self.assertRaisesRegex(ValueError, "sort"):
            library_browser.query_library_page(
                self.db, self.library, {"sort": "surprise"}
            )

    def test_paths_outside_library_are_not_exposed_as_urls(self) -> None:
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"image")
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "INSERT INTO images ("
                "path, filename, source, purity, enrichment_status, indexed_at"
                ") VALUES (?, 'outside.jpg', 'unknown', 'sfw', 'ok', 'now')",
                (str(outside),),
            )
            conn.commit()
        finally:
            conn.close()

        page = library_browser.query_library_page(
            self.db, self.library, {"rating": "sfw", "limit": "10"}
        )
        item = next(item for item in page["items"] if item["filename"] == "outside.jpg")
        self.assertIsNone(item["url"])

    def test_verification_status_detects_post_verify_db_change(self) -> None:
        reports = self.root / "reports"
        verify_dir = reports / "maintenance-20260719T000000Z-test"
        verify_dir.mkdir(parents=True)
        verify = verify_dir / "verify.json"
        verify.write_text(
            json.dumps({"ok": True, "status": "ok", "counts": {"indexed_images": 5}}),
            encoding="utf-8",
        )
        now = time.time()
        os.utime(self.db, (now - 10, now - 10))
        os.utime(verify, (now, now))
        current = library_browser.latest_verification_status(reports, self.db)
        self.assertTrue(current["verified"])

        os.utime(self.db, (now + 10, now + 10))
        changed = library_browser.latest_verification_status(reports, self.db)
        self.assertFalse(changed["verified"])
        self.assertIn("changed after", changed["reason"])


if __name__ == "__main__":
    unittest.main()
